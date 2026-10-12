# AI-Kubernetes 개발 태스크 장부

> 이 문서는 [정본 SQLite 데이터베이스](../planning/backlog.sqlite3)에서 생성한 Markdown 스냅샷입니다. 태스크·요구사항·결정·출처의 기준은 DB이며, 이 파일의 수동 수정은 다음 생성에서 덮어써집니다.

- 작성일 (UTC): `2026-10-12T05:36:30Z`
- 수정일 (UTC): `2026-10-12T06:02:41Z`
- 검토일 (UTC): `2026-10-12T06:02:41Z`
- SQLite `user_version`: `1` · 메타데이터 스키마 버전: `1`
- 태스크 82개 · 요구사항 17개 · 결정 12개 · 출처 13개
- 상태: 미착수 82개 · 진행 중 0개 · 진행 차단 0개 · 완료 0개 · 취소 0개

## 유지보수 방법

저장소 루트에서 Python 표준 라이브러리만으로 실행합니다. 상태 변경은 이유와 함께 기록하며, 완료로 바꾸려면 완료 기준과 선행 작업 충족 및 `task_evidence`의 증거가 필요합니다. 계획 등록 자체는 구현 완료의 근거가 아닙니다.

```bash
python tools/backlog.py validate
python tools/backlog.py list --ready
python tools/backlog.py status AIK-001 in_progress --note "작업 시작"
```

기본 DB에 대한 `status` 성공 시 SQL과 이 보고서도 갱신합니다. SQL로 DB를 직접 수정했거나 증거를 추가했다면 다음 두 명령으로 검증된 스냅샷을 다시 생성한 뒤 함께 커밋합니다.

```bash
python tools/backlog.py export
python tools/backlog.py report
```

`--db`로 다른 DB를 선택할 수 있습니다. `report --output 경로.md`는 출력 경로를 바꿉니다. `restore --source planning/backlog.sql --output 새경로.sqlite3`는 기존 파일을 덮어쓰지 않고 복원·검증합니다.

## 단계별 현황

| 단계 | 범위 | 태스크 | 미착수 | 진행 중 | 차단 | 완료 | 취소 | 착수 가능 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| M0 · 통합 계약·Windows 기반 결정 | 첫 출시 | 8 | 8 | 0 | 0 | 0 | 0 | 3 |
| M1 · 인증 보관·멀티벤더 자원 연결 | 첫 출시 | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| M2 · 총괄 에이전트·AGS/VM 실행 통합 | 첫 출시 | 24 | 24 | 0 | 0 | 0 | 0 | 0 |
| M3 · Windows GUI 실제 연결 | 첫 출시 | 12 | 12 | 0 | 0 | 0 | 0 | 0 |
| M4 · Windows 첫 배포 검증 | 첫 출시 | 8 | 8 | 0 | 0 | 0 | 0 | 0 |
| M5 · 탄력 운영·원격 PEER 연합 | 후속 개발 | 11 | 11 | 0 | 0 | 0 | 0 | 0 |
| M6 · 추가 자원·조직·배포 확장 | 향후 확장 | 5 | 5 | 0 | 0 | 0 | 0 | 0 |

착수 가능 수는 DB에 기록된 선행 작업이 모두 완료된 `todo` 태스크를 셉니다.

## M0 · 통합 계약·Windows 기반 결정

**범위:** 첫 출시 (`first_release`)

**단계 완료 조건:** 엔진 책임·지원 환경·GUI 구조·설치형 계약을 확정하고 런타임 기반을 준비한다.

### AIK-001

**AGS·VM 책임·설치형 인터페이스 기준선 확정**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Integration |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 1 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

현재 배포물의 공개 command/query, manifest, schema, 오류와 evidence 계약을 고정하고 문서 상태 차이를 해소한다.

- **산출물:** 버전별 integration-contract·capability/오류 매핑표·판본과 digest가 있는 지원 matrix
- **검증 방법:** 고정 SHA 문서와 실제 설치형 artifact를 대조한 검토 기록
- **선행 작업:** 없음
- **요구사항:** [D04](#d04), [D08](#d08), [U08](#u08)
- **출처:** [S-AGS](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md) ([색인](#s-ags)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core)), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** [DEC04](#dec04) (확정), [DEC12](#dec12) (제안)

**완료 기준**

1. VM Goal/Plan/Task/Attempt와 AIK lease/placement의 단일 소유자 및 상호 참조 필드가 명시된다.
2. AGS manifest 실제 경로·entrypoint·digest와 VM schema4/façade 계약을 확인하며 GUI schema2 예시 차이를 기록한다.
3. 문서상 구현·실행 미검증·호환 미완료를 구분한다. 첫 출시 matrix에 route_id·provider·auth_kind·role·required/optional·adapter 구현 상태·qualification 상태·사유·관측 시각·evidence_ref와 revision/digest를 기록한다. 두 벤더 및 API/지원 구독·총괄/worker 도구 실행이라는 DEC12 제안 범위와 필수 조합의 대응을 명시하며 범위 또는 upstream 변경 시 결정·영향·재검증 근거를 남긴다.

**기록된 증거**

기록 없음.

### AIK-002

**Windows GUI 스택과 엔진 sidecar 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop |
| 작업 유형 | 결정 (`decision`) |
| 에픽 | E01 · 제품·통합 기반 |
| 실행 순서 | 2 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

VM GUI 자산을 검토하고 Windows에서 GUI·런타임·시크릿 호출이 연결되는 작은 spike로 구현 스택을 결정한다.

- **산출물:** GUI 스택 ADR와 폐기 가능한 Windows spike
- **검증 방법:** 지원 후보별 설치성·IPC·worker 기동·메모리 비교
- **선행 작업:** 없음
- **요구사항:** [D07](#d07), [U03](#u03), [U04](#u04), [U08](#u08)
- **출처:** [S-USER](#s-user), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** [DEC01](#dec01) (확정), [DEC07](#dec07) (미정)

**완료 기준**

1. VM React/TypeScript 화면·EngineClient 재사용 범위와 Tauri/대안의 선택 이유를 적는다.
2. 창 표시→typed IPC→테스트 worker 호출→OS vault 왕복을 Windows에서 재현한다.
3. AIK 스택·엔진 runtime 버전·bundle 전략을 ADR에 명시하고 mock 성공을 제품 연동 성공으로 기록하지 않는다.

**기록된 증거**

기록 없음.

### AIK-003

**Windows 지원 환경·설치·서명 방식 결정**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Release |
| 작업 유형 | 결정 (`decision`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 3 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

첫 Windows 배포의 지원 버전, CPU, 설치 형식과 업데이트·서명·사용자 데이터 정책을 선택한다.

- **산출물:** Windows support matrix와 packaging ADR
- **검증 방법:** 깨끗한 후보 Windows 환경의 설치 spike 및 문서 검토
- **선행 작업:** 없음
- **요구사항:** [D07](#d07), [U04](#u04)
- **출처:** [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC01](#dec01) (확정), [DEC08](#dec08) (미정)

**완료 기준**

1. 지원 Windows 버전·CPU·최소 자원과 선택한 설치 형식을 명시한다.
2. 개발 도구가 없는 환경에서 필요한 runtime을 어떻게 공급할지 검증한다.
3. 서명·업데이트 신뢰·제거 후 데이터 처리 기준을 정하며 macOS/Linux를 첫 출시 gate로 넣지 않는다.

**기록된 증거**

기록 없음.

### AIK-004

**GUI·총괄·실행·시크릿 프로세스와 IPC 경계**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop backend |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E01 · 제품·통합 기반 |
| 실행 순서 | 4 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

GUI는 제한된 command/query client로 두고 실행과 비밀 조회를 backend broker에서 통제한다. 실행 경로별 OS principal·trust profile·실제 권한과 격리 보장 범위를 명시한다.

- **산출물:** 프로세스 구획·버전형 IPC 스키마·명령 라우터
- **검증 방법:** 잘못된 발신자·입력·권한에 대한 IPC 통합 검증
- **선행 작업:** [AIK-001](#aik-001), [AIK-002](#aik-002)
- **요구사항:** [D01](#d01), [D04](#d04), [U03](#u03)
- **출처:** [S-CREDREAD](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreada) ([색인](#s-credread)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** [DEC04](#dec04) (확정), [DEC07](#dec07) (미정)

**완료 기준**

1. GUI renderer가 arbitrary shell·전체 filesystem·raw secret API에 직접 접근하지 않는다. API 제한 도구 경로와 trusted same-user native 경로의 OS principal·trust profile을 구분하며 앱 IPC 제한을 OS 프로세스 격리로 광고하지 않는다.
2. 명령마다 schema·발신자·workspace·request_id를 검증하고 응답에 민감 값을 넣지 않는다.
3. 호환되지 않는 frontend/backend 버전은 구체적인 연결 오류로 표시한다.

**기록된 증거**

기록 없음.

### AIK-005

**AIK 설정·placement·lease 저장소와 마이그레이션**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Storage |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E01 · 제품·통합 기반 |
| 실행 순서 | 5 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

제품 runtime 저장소를 개발 태스크 DB와 분리하고 VM 원장을 복제하지 않는 AIK 상태를 영속화한다.

- **산출물:** runtime DB schema·migration·참조 repository
- **검증 방법:** transaction crash와 버전 변경 rollback 검증
- **선행 작업:** [AIK-001](#aik-001)
- **요구사항:** [D01](#d01), [D03](#d03), [D04](#d04)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** [DEC04](#dec04) (확정)

**완료 기준**

1. pool/credential_ref/profile/placement/lease와 VM/AGS 외부 ID를 저장하고 실비밀·VM 권위 Task 사본은 저장하지 않는다.
2. 원자적 쓰기와 revision 충돌 처리가 동작하고 손상/마이그레이션 실패 시 원본을 보전한다.
3. 제품 운영 데이터가 planning/backlog.sqlite3 또는 Git 추적 경로에 기록되지 않는다.

**기록된 증거**

기록 없음.

### AIK-006

**로컬 supervisor와 단일 총괄 인스턴스**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Supervisor |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E01 · 제품·통합 기반 |
| 실행 순서 | 6 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

GUI 창과 런타임 수명을 분리하고 사용자 세션 내에서 총괄·sidecar 중복 기동을 막는다.

- **산출물:** supervisor·instance lock·재연결 endpoint
- **검증 방법:** 동시 앱 실행·창 재열기·강제 종료 재기동 검증
- **선행 작업:** [AIK-004](#aik-004), [AIK-005](#aik-005)
- **요구사항:** [D03](#d03), [D04](#d04), [U02](#u02)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. 같은 사용자/workspace에서 GUI를 두 번 열어도 동일 총괄 인스턴스에 연결한다.
2. 창 닫기·작업 일시정지·앱 완전 종료의 동작을 분리한다.
3. 앱 종료 시 신규 배치를 중지하고 실제 진행 중 job은 재시작 때 관측 대상으로 남긴다.

**기록된 증거**

기록 없음.

### AIK-007

**프로젝트·연결·프로필 설정과 권한 기본값**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Configuration |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E01 · 제품·통합 기반 |
| 실행 순서 | 7 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

workspace별 설정 schema와 UI가 사용할 조회·변경 명령을 마련한다.

- **산출물:** 설정 repository·typed settings API
- **검증 방법:** 설정 재시작 유지·잘못된 참조·revision 충돌 검증
- **선행 작업:** [AIK-004](#aik-004), [AIK-005](#aik-005)
- **요구사항:** [D01](#d01), [D05](#d05), [U01](#u01), [U02](#u02)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. workspace·연결·총괄/worker 프로필·동시성/예산 값을 구분해 저장한다.
2. 다른 workspace의 연결 참조와 유효하지 않은 설정을 검증 단계에서 차단한다.
3. 설정 내보내기는 비밀 값을 제외하고 import 후 인증이 필요한 연결을 표시한다.

**기록된 증거**

기록 없음.

### AIK-008

**AGS·VM 설치형 적합성 진단과 upstream 결함 목록**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Integration |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 8 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

기준선 문서에 남은 A2 gate·관측·freeze 상태를 설치형 진단으로 확인하고 필요한 upstream 작업을 분리한다.

- **산출물:** compatibility report·진단 command·upstream blocker 목록
- **검증 방법:** 빈 임시 workspace의 문서 대조와 설치형 conformance 실행
- **선행 작업:** [AIK-001](#aik-001)
- **요구사항:** [D06](#d06), [D08](#d08), [U08](#u08)
- **출처:** [S-AGS](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md) ([색인](#s-ags)), [S-AGS-ATTEST](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md) ([색인](#s-ags-attest)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** [DEC04](#dec04) (확정), [DEC12](#dec12) (제안)

**완료 기준**

1. 현재 compatible/incompatible/unverified 상태와 manifest·버전·digest를 진단 결과로 남긴다.
2. 첫 출시 지원 matrix에 따라 A2 호환 plugin과 각 runtime의 model/effort 관측을 AIK-081/082에 연결한다. 개별 미지원 경로와 다른 qualified 경로의 상태를 분리한다.
3. 문서·mock 통과만으로 실제 gate를 PASS 처리하지 않으며 실행불가 원인을 GUI가 소비할 수 있게 반환한다.

**기록된 증거**

기록 없음.

## M1 · 인증 보관·멀티벤더 자원 연결

**범위:** 첫 출시 (`first_release`)

**단계 완료 조건:** 후보 adapter 코드를 구현하고, 같은 지원 matrix의 필수 두 벤더 API/지원 구독 인증 경로와 vault를 실제 Windows에서 검증한다. 선택 미지원 경로는 별도 결과로 유지한다.

### AIK-009

**Windows 사용자 범위 시크릿 vault 구현**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Credential broker |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E02 · 시크릿·인증 |
| 실행 순서 | 9 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

API 키와 app 소유 비밀을 Windows 사용자 저장소에 보관하고 opaque credential_ref로 접근한다.

- **산출물:** vault backend·CRUD·OS 사용자 경계
- **검증 방법:** 실제 Windows vault 왕복·다른 사용자·재시작·오류 검증
- **선행 작업:** [AIK-002](#aik-002), [AIK-003](#aik-003), [AIK-004](#aik-004), [AIK-005](#aik-005)
- **요구사항:** [D01](#d01), [U01](#u01), [U04](#u04)
- **출처:** [S-CRED](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritea) ([색인](#s-cred)), [S-CREDREAD](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreada) ([색인](#s-credread)), [S-DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) ([색인](#s-dpapi)), [S-USER](#s-user)
- **관련 결정:** [DEC03](#dec03) (확정), [DEC09](#dec09) (제안)

**완료 기준**

1. Credential Manager 또는 사용자 범위 DPAPI 후보를 검증해 선택하고 머신 공유 범위를 기본으로 사용하지 않는다. 저장 보호·다른 OS 사용자 경계와 동일 사용자 native 프로세스의 직접 접근 범위를 ADR에 명시한다.
2. 저장·교체·삭제와 앱 재시작 후 참조 회수가 동작한다.
3. SQLite·설정·로그·지원 번들에서 테스트 비밀 평문이 검출되지 않으며 저장소 접근 실패를 정확히 반환한다.

**기록된 증거**

기록 없음.

### AIK-010

**다중 계정·구독·API 연결 metadata 관리**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Connections |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E02 · 시크릿·인증 |
| 실행 순서 | 10 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

같은 벤더의 여러 계정과 구독/API 실행 경로를 별도 connection으로 등록하고 resource와 연결한다.

- **산출물:** ConnectionProfile schema·CRUD API
- **검증 방법:** 다중 계정 등록·이름 변경·중복·삭제 시 참조 검증
- **선행 작업:** [AIK-007](#aik-007), [AIK-009](#aik-009)
- **요구사항:** [D01](#d01), [U01](#u01), [U05](#u05)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** [DEC03](#dec03) (확정)

**완료 기준**

1. provider/account label/auth_kind/execution_kind/credential_ref와 확인 시각을 저장한다.
2. 동일 벤더 구독/API를 별도 등록하고 connection_id별 공식 호스트·프로필·비민감 계정 binding revision을 연결한다. 등록한 계정과 실행할 인증 문맥의 일치를 검증한다.
3. 이름 변경으로 기존 task provenance가 바뀌지 않고 사용 중 연결 삭제는 영향 범위를 표시한다.

**기록된 증거**

기록 없음.

### AIK-011

**인증·실행 adapter 공통 계약**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Provider adapters |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 11 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

API 키 및 공식 구독 호스트의 sign-in/status/sign-out과 실행 능력을 공통 형식으로 노출한다.

- **산출물:** ProviderAdapter 인터페이스·오류/capability 스키마
- **검증 방법:** 가짜 adapter와 실제 호스트 계약 대조 검증
- **선행 작업:** [AIK-001](#aik-001), [AIK-010](#aik-010)
- **요구사항:** [D02](#d02), [D08](#d08), [U05](#u05)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. auth 연결 성공과 job 실행·관측·중단 지원을 별도 capability로 반환한다.
2. 공식 호스트의 계정별 인증 context 격리 지원을 capability로 반환한다. 격리 불가 호스트의 충돌 동시 사용과 다른 계정으로의 묵시적 전환을 막고 구독 인증을 API 키로 변환하지 않는다.
3. 벤더 고유 오류·rate limit·확인 불가능 상태를 보존하는 표준 오류를 정의한다.

**기록된 증거**

기록 없음.

### AIK-012

**OpenAI API 연결 adapter 구현·계약 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | OpenAI API adapter |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 12 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

공식 API 키 연결을 공통 자원·usage·실행 관측 계약으로 변환하는 adapter 코드를 구현하고 기존 구현 재사용 범위를 확인한다. 이 태스크의 구현 완료와 실제 계정 qualification을 구분한다.

- **산출물:** OpenAI API adapter 코드·provider protocol fixture·구현 적합성 결과
- **검증 방법:** 가짜 비밀과 protocol fixture로 성공/실패 계약 검증; 실제 인증은 AIK-022, runtime/도구 실행은 AIK-082/044에서 검증
- **선행 작업:** [AIK-009](#aik-009), [AIK-011](#aik-011)
- **요구사항:** [D02](#d02), [U01](#u01), [U05](#u05)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. 키 등록·인증 확인·모델 호출·교체·해제의 코드 경로를 구현하고 성공 및 실패 protocol fixture를 통과한다. 미지원 응답만 반환하는 stub은 구현 완료가 아니다.
2. provider request/job·model·usage와 도구 요청/결과 메시지를 공통 계약으로 변환하며 AIK-035의 bounded agent tool loop에 연결 가능하다. fixture 결과를 provider 실측으로 기록하지 않는다.
3. 잘못된 키/권한/모델을 실행 가능 자원으로 광고하지 않고 구독 잔여량과 API 과금을 구분한다. adapter 구현 상태와 route qualification 상태를 별도로 보관하며 실제 지원은 AIK-022/082의 근거로만 판정한다.

**기록된 증거**

기록 없음.

### AIK-013

**Anthropic API 연결 adapter 구현·계약 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Anthropic API adapter |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 13 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

공식 API 키 연결을 공통 자원·usage·실행 관측 계약으로 변환하는 adapter 코드를 구현한다. 실제 계정 지원 여부는 별도 qualification으로 판정한다.

- **산출물:** Anthropic API adapter 코드·provider protocol fixture·구현 적합성 결과
- **검증 방법:** 가짜 비밀로 인증·호출·usage·도구/오류 fixture 검증; 실제 인증은 AIK-022, runtime/도구 실행은 AIK-082/044에서 검증
- **선행 작업:** [AIK-009](#aik-009), [AIK-011](#aik-011)
- **요구사항:** [D02](#d02), [U01](#u01), [U05](#u05)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. 키 등록·인증 확인·모델 호출·교체·해제의 코드 경로를 구현하고 성공 및 실패 protocol fixture를 통과한다. 미지원 응답만 반환하는 stub은 구현 완료가 아니다.
2. provider model·usage·cache 및 API 도구 요청/결과 메시지를 공통 계약으로 변환한다. 잘린 결과를 완료로 승격하지 않고 AIK-035의 agent tool loop와 연결 가능하며 fixture 결과를 실측으로 기록하지 않는다.
3. 401/429/timeout과 모델 불가 상태를 공통 오류로 노출한다. adapter 구현 상태와 route qualification 상태를 분리하고 실제 지원은 AIK-022/082의 근거로만 판정한다.

**기록된 증거**

기록 없음.

### AIK-014

**Codex 구독 호스트 인증 bridge 구현·계약 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Codex host bridge |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 14 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

공식 Codex 호스트 인증과 기존 VM runtime을 연결하는 bridge를 구현한다. 호스트 설치·인증·governance 미지원은 경로별 qualification 상태이며 bridge 코드의 구현 상태와 구분한다.

- **산출물:** Codex host auth bridge 코드·host protocol fixture·connection capability
- **검증 방법:** host fixture로 공식 인증 시작·상태·만료·해제·계정 변경 계약 검증; 실제 Windows 인증/실행은 AIK-022/082/044에서 검증
- **선행 작업:** [AIK-008](#aik-008), [AIK-011](#aik-011)
- **요구사항:** [D08](#d08), [U01](#u01), [U05](#u05), [U08](#u08)
- **출처:** [S-AGS](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md) ([색인](#s-ags)), [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. GUI command에 대응하는 공식 인증 시작·상태 회수·재연결·해제 코드 경로를 구현하고 성공/실패 host fixture를 통과한다. unsupported만 반환하는 stub이나 fixture 통과를 실제 로그인 성공으로 기록하지 않는다.
2. 공식 호스트 소유 인증은 host handle로 연결하며 원문 토큰을 범용 API 비밀로 재사용하지 않는다. host absent/unsupported와 인증 만료를 별도 capability로 반환한다.
3. 기존 VM runtime을 재사용하고 실행 전 비민감 계정 binding revision을 대조한다. 외부 로그인 변경은 stale/재연결로 표시하며 bridge 구현·실제 auth 성공·governed 적합성을 구분한다. 라이브 근거는 AIK-022/082로 연결한다.

**기록된 증거**

기록 없음.

### AIK-015

**Claude Code 구독 호스트 인증 bridge 구현·계약 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Claude host bridge |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 15 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

공식 Claude Code 인증과 기존 VM CLI runtime을 자원 등록·재연결 계약으로 연결하는 bridge를 구현한다. 코드 구현과 실제 Windows 호스트 qualification을 구분한다.

- **산출물:** Claude host auth bridge 코드·host protocol fixture·connection capability
- **검증 방법:** host fixture로 공식 인증·실행 요청·만료·해제·계정 변경 계약 검증; 실제 Windows 인증/실행은 AIK-022/082/044에서 검증
- **선행 작업:** [AIK-008](#aik-008), [AIK-011](#aik-011)
- **요구사항:** [D02](#d02), [U01](#u01), [U05](#u05), [U08](#u08)
- **출처:** [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. 공식 인증 시작·상태 확인·실행 요청·해제·재연결의 코드 경로를 구현하고 성공/실패 host fixture를 통과한다. unsupported만 반환하는 stub이나 fixture 통과를 실제 실행 성공으로 기록하지 않는다.
2. 호스트의 로그인 비밀을 이중 복제하지 않고 host handle과 capability를 보관한다. host absent/unsupported와 인증 만료를 구분한다.
3. configured catalog와 runtime 실측을 분리하고 선택 계정 binding을 검증한다. 외부 로그인 변경·계정 격리 불가를 capability/재연결 필요로 표시한다. bridge 구현과 라이브 지원 판정을 분리하고 실제 근거는 AIK-022/082로 연결한다.

**기록된 증거**

기록 없음.

### AIK-016

**인증 만료·키 회전·잠금·로그아웃 수명**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Credential broker |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E02 · 시크릿·인증 |
| 실행 순서 | 16 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

연결 자격이 바뀌었을 때 신규 배치와 진행 중 작업을 구분해 처리한다.

- **산출물:** auth state machine·재인증/잠금 이벤트
- **검증 방법:** 만료·동시 회전·잠금 중 실행·연결 삭제 경합 검증
- **선행 작업:** [AIK-009](#aik-009), [AIK-010](#aik-010), [AIK-011](#aik-011)
- **요구사항:** [D01](#d01), [D04](#d04), [U01](#u01)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. 만료/잠금/해제 자원은 신규 배치에서 제외하고 재인증 후 재검증한다.
2. 키 교체 뒤 새 실행은 새 참조 버전을 사용하며 이전 실행은 실제 상태를 회수한다.
3. 사용자가 설정한 vault 잠금·Windows 세션 잠금 정책이 동작하고 강제 로그아웃을 완료/환불로 간주하지 않는다.

**기록된 증거**

기록 없음.

### AIK-017

**프롬프트·로그·오류·내보내기 시크릿 제거**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Credential broker |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E02 · 시크릿·인증 |
| 실행 순서 | 17 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

비밀이 통과할 수 있는 모든 제품 출력 경로에서 실제 값과 민감 헤더를 제거한다.

- **산출물:** redaction policy·sanitized error envelope·검증 fixture
- **검증 방법:** 식별 가능한 가짜 비밀로 전체 출력 경로 검사
- **선행 작업:** [AIK-004](#aik-004), [AIK-009](#aik-009), [AIK-011](#aik-011)
- **요구사항:** [D01](#d01), [U01](#u01)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) ([색인](#s-dpapi))
- **관련 결정:** 없음

**완료 기준**

1. prompt/IPC response/PEER/evidence/log/crash support bundle에 테스트 비밀이 남지 않는다.
2. 부분 키·Authorization 헤더·중첩 오류 객체·URL query에 대한 제거 규칙을 검증한다.
3. redaction 후에도 connection/task/request ID로 오류와 감사 이력을 추적할 수 있다.

**기록된 증거**

기록 없음.

### AIK-018

**작업 범위 credential broker와 실행 시점 주입**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Credential broker |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E02 · 시크릿·인증 |
| 실행 순서 | 18 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

어댑터가 인가된 연결에 한해 호출 시점에 필요한 비밀을 해석하도록 한다.

- **산출물:** scoped secret resolution API·adapter injection
- **검증 방법:** 다른 workspace/임의 renderer/종료된 lease의 접근 검증
- **선행 작업:** [AIK-004](#aik-004), [AIK-009](#aik-009), [AIK-010](#aik-010), [AIK-017](#aik-017)
- **요구사항:** [D01](#d01), [D05](#d05), [U01](#u01)
- **출처:** [S-CREDREAD](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreada) ([색인](#s-credread)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 총괄과 worker에는 connection_id 또는 credential_ref만 전달한다.
2. 허용된 adapter·workspace·작업 범위가 일치할 때만 broker 경로에서 비밀을 해석하고 임의 열람 요청을 거절한다. 같은 OS 사용자 native 프로세스의 직접 저장소 접근까지 이 검사로 격리된다고 주장하지 않으며 해당 경로의 trust profile을 연결한다.
3. 비밀을 명령행 인수나 지속 설정파일에 남기지 않고 자식 프로세스 전달·정리 범위를 검증한다.

**기록된 증거**

기록 없음.

### AIK-019

**ResourcePool 레지스트리·건강 상태·발견**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Resource registry |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 19 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

공통 연결·capability 계약에 따라 모델/host/tools와 현재 실행 가능 상태를 광고한다. 공통 registry 개발은 모든 후보 adapter의 완성을 기다리지 않으며 구현 여부와 실제 qualification을 분리한다.

- **산출물:** resource inventory·health probe·TTL 갱신
- **검증 방법:** 공통 fake adapter로 qualified/unsupported/NOT_RUN/stale와 다중 계정 상태를 검증; 실제 경로는 AIK-022/082에서 qualification
- **선행 작업:** [AIK-011](#aik-011), [AIK-016](#aik-016)
- **요구사항:** [D02](#d02), [D05](#d05), [U05](#u05)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. pool_id와 connection_id를 분리하고 같은 모델의 구독/API 경로를 각각 조회한다.
2. adapter 구현 상태·auth 계정 binding·runtime 지원·governance 적합성·최종 확인 시각·matrix revision을 별도 필드로 노출한다. 외부 로그인 변경은 재검증하며 선택 경로 unsupported가 다른 qualified 경로를 덮지 않는다. 구현 완료만으로 실행 가능 광고를 하지 않는다.
3. 오래되거나 확인할 수 없는 값은 unknown/stale로 표시하며 0이나 무제한 용량으로 만들지 않는다.

**기록된 증거**

기록 없음.

### AIK-020

**모델·추론 수준·도구 capability 조회와 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Model inventory |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 20 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

계정별 이용 가능한 모델과 runtime 옵션을 조회하여 총괄·worker 설정의 유효 조합을 만든다.

- **산출물:** model inventory·capability validator
- **검증 방법:** 변경된 모델 목록·지원 안 되는 effort/tool 조합 검증
- **선행 작업:** [AIK-019](#aik-019)
- **요구사항:** [D02](#d02), [U02](#u02), [U05](#u05)
- **출처:** [S-AGS-ATTEST](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md) ([색인](#s-ags-attest)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. 모델 이름을 제품 코드에 고정하지 않고 자원별 목록/검증된 수동 등록을 지원한다.
2. 추론 수준·도구·context 제한 등 확인 가능한 항목의 출처와 시각을 기록한다.
3. requested/provider_observed/configured_catalog/local_derived를 구분하며 이름이 같다고 capability를 동일 취급하지 않는다.

**기록된 증거**

기록 없음.

### AIK-021

**workspace별 연결 사용 범위·안전한 설정 이동**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Connections |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E02 · 시크릿·인증 |
| 실행 순서 | 21 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

여러 프로젝트가 연결을 공유하는 범위와 설정의 import/export 정책을 구현한다.

- **산출물:** connection scope policy·설정 export/import
- **검증 방법:** 다른 workspace·다른 Windows 사용자/기기의 복원 검증
- **선행 작업:** [AIK-010](#aik-010), [AIK-018](#aik-018)
- **요구사항:** [D01](#d01), [D05](#d05), [U01](#u01)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) ([색인](#s-dpapi))
- **관련 결정:** 없음

**완료 기준**

1. workspace가 사용할 연결을 명시하고 미허용 connection 참조를 차단한다.
2. 내보낸 설정은 API 키나 공식 호스트 토큰을 포함하지 않는다.
3. 새 사용자/기기에서 해석할 수 없는 비밀 참조는 재인증 필요 상태로 복원한다.

**기록된 증거**

기록 없음.

### AIK-022

**Windows 멀티벤더 인증·vault qualification**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Windows qualification |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E02 · 시크릿·인증 |
| 실행 순서 | 22 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

AIK-001 지원 matrix의 필수 인증 경로를 실제 Windows 환경에서 검증한다. 후보 adapter 구현과 라이브 인증 qualification을 구분하고 선택 경로의 미지원은 독립 결과로 보관한다.

- **산출물:** matrix revision/digest별 auth/vault qualification·경로별 live evidence·재현 절차
- **검증 방법:** 실제 Windows 사용자 환경과 가짜 비밀의 실패 경로 검증
- **선행 작업:** [AIK-012](#aik-012), [AIK-013](#aik-013), [AIK-014](#aik-014), [AIK-015](#aik-015), [AIK-016](#aik-016), [AIK-017](#aik-017), [AIK-018](#aik-018), [AIK-019](#aik-019), [AIK-020](#aik-020), [AIK-021](#aik-021)
- **요구사항:** [D01](#d01), [D06](#d06), [D08](#d08), [U01](#u01), [U04](#u04)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. 동일 matrix revision의 필수 API/지원 구독 인증 경로마다 실제 Windows 등록·호출·회전·해제·재연결을 통과한다. 같은 벤더의 두 연결과 외부 로그인 변경에서 다른 계정으로 묵시 실행되지 않는다. adapter fixture만으로 이 기준을 통과하지 않는다.
2. 다른 사용자·잠금·손상된 참조·인증 만료·로그 제거 조건을 합성 비밀로 검증한다. 실제 계정 비밀은 장부/검증 산출물에 저장하지 않는다.
3. auth 성공과 governed 실행 준비 여부를 별도 판정한다. 선택/미지원 경로의 사유·관측 시각·NOT_RUN 및 근거를 기록하며 필수 경로가 미지원/미검증이면 이 태스크는 완료할 수 없다. matrix 변경은 결정·영향·재검증 기록을 요구한다.

**기록된 증거**

기록 없음.

## M2 · 총괄 에이전트·AGS/VM 실행 통합

**범위:** 첫 출시 (`first_release`)

**단계 완료 조건:** 총괄이 같은 지원 matrix의 필수 runtime/역할과 두 벤더 worker를 실제 실행·검증·회수하고 VM/AGS 및 재시작/한도 경계를 보존한다.

### AIK-023

**총괄·worker 역할별 실행 프로필과 모델 지정**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Director |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E04 · 총괄 에이전트 |
| 실행 순서 | 23 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

총괄의 connection/model/effort/tool/budget/fallback을 설정하고 worker/검토 역할과 독립 유지한다.

- **산출물:** DirectorProfile·RoleProfile schema·검증 명령
- **검증 방법:** 다중 벤더 프로필·옵션 불일치·역할 분리 검증
- **선행 작업:** [AIK-007](#aik-007), [AIK-020](#aik-020)
- **요구사항:** [D02](#d02), [D05](#d05), [U02](#u02)
- **출처:** [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC02](#dec02) (확정)

**완료 기준**

1. 총괄 실행 자원·모델·지원 effort·도구·호출 한도와 허용 대체 프로필을 저장한다.
2. 총괄 설정을 바꾸어도 worker/검토자 프로필은 덮어쓰지 않는다.
3. 실행 직전 실제 capability를 검증하고 요청 프로필과 실제 binding/provenance를 함께 남긴다.

**기록된 증거**

기록 없음.

### AIK-024

**총괄 에이전트 bootstrap·시작·정지**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Director |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E04 · 총괄 에이전트 |
| 실행 순서 | 24 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

사용자가 지정한 자원으로 총괄을 실행하고 미설정 상태에서 로그인·모델 선택으로 이어지는 초기 흐름을 만든다.

- **산출물:** director bootstrap·runtime binding·health endpoint
- **검증 방법:** 미설정/정상/인증불가 상태와 중복 시작 검증
- **선행 작업:** [AIK-006](#aik-006), [AIK-018](#aik-018), [AIK-023](#aik-023)
- **요구사항:** [D02](#d02), [D03](#d03), [U02](#u02)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. worker 배치 없이도 총괄 연결을 먼저 검증·시작할 수 있다.
2. 프로필 미설정 또는 실행불가 사유가 있으면 설정이 필요한 항목을 명확히 반환한다.
3. 중복 시작을 막고 시작/정지/실패와 실제 모델·effort 관측을 기록한다.

**기록된 증거**

기록 없음.

### AIK-025

**총괄 inbox·callback·wake 큐**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Director event loop |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E04 · 총괄 에이전트 |
| 실행 순서 | 25 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

사용자 지시와 AGS/VM 이벤트를 영속 수신하고 필요한 때 총괄을 깨운다.

- **산출물:** durable inbox/outbox·consumer·wake 정책
- **검증 방법:** 중복·순서 역전·재시작·빈 큐 유휴 검증
- **선행 작업:** [AIK-005](#aik-005), [AIK-024](#aik-024)
- **요구사항:** [D03](#d03), [D04](#d04), [U02](#u02)
- **출처:** [S-AGS](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md) ([색인](#s-ags)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 메시지 ID·목표 ID·event cursor를 기록하고 동일 이벤트를 중복 처리하지 않는다.
2. 실행 결과·오류·사용자 추가 지시에 반응하며 유휴 상태에서 무한 model 호출을 하지 않는다.
3. 앱 재시작 후 미처리 항목을 회수하고 처리 ACK와 작업 완료를 구분한다.

**기록된 증거**

기록 없음.

### AIK-031

**VM EngineApplication command/query bridge**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | VM bridge |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 26 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

GUI와 총괄이 VM façade로 Goal 준비·인가·실행·관측·상태·보고를 요청하게 한다.

- **산출물:** typed VM client·버전 handshake·오류 매핑
- **검증 방법:** VM 공식 façade 계약 fixture 및 설치형 호출
- **선행 작업:** [AIK-001](#aik-001), [AIK-004](#aik-004), [AIK-008](#aik-008)
- **요구사항:** [D04](#d04), [D08](#d08), [U08](#u08)
- **출처:** [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core)), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** [DEC04](#dec04) (확정)

**완료 기준**

1. Goal/Plan/Task 수정은 공개 command만 사용하고 VM DB를 AIK가 직접 SQL로 변경하지 않는다.
2. request/goal/task/attempt ID, revision, digest, history cursor와 기존 인가를 보존한다.
3. STALE_EXECUTION_INPUT·EXTERNAL_UNKNOWN 등 원래 오류 의미를 유지한다.

**기록된 증거**

기록 없음.

### AIK-029

**총괄·worker 도구 권한과 실행 인가 연결**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Authorization |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E07 · 관측·감사·비용 |
| 실행 순서 | 27 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

사용자→총괄→worker 위임에서 기존 인가 범위를 전달하고 범위 확대를 식별한다.

- **산출물:** scoped tool gateway·authorization mapper
- **검증 방법:** 동일 인가 재사용·범위 확대·stale revision 검증
- **선행 작업:** [AIK-004](#aik-004), [AIK-007](#aik-007), [AIK-018](#aik-018), [AIK-031](#aik-031)
- **요구사항:** [D04](#d04), [D05](#d05), [U02](#u02), [U08](#u08)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** [DEC04](#dec04) (확정)

**완료 기준**

1. 이미 허용된 작업은 기존 범위를 전달하며 불필요한 반복 승인을 요구하지 않는다.
2. 작업 대상·효과·입력 revision이 인가 범위를 벗어나는 경우 VM/AGS 계약대로 처리한다.
3. 총괄·worker·원격 peer가 자신에게 새 권한을 부여하거나 완료 판정을 덮어쓸 수 없다.

**기록된 증거**

기록 없음.

### AIK-026

**총괄 Goal→위임→회수→보고 루프**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Director |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E04 · 총괄 에이전트 |
| 실행 순서 | 28 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

총괄이 자연어 목표를 VM command에 연결하고 작업 결과·검증 상태를 보고하도록 한다.

- **산출물:** controller command loop·bounded run policy
- **검증 방법:** VM façade fixture와 이후 실제 E2E에서 상태별 행동 검증
- **선행 작업:** [AIK-025](#aik-025), [AIK-029](#aik-029), [AIK-031](#aik-031)
- **요구사항:** [D03](#d03), [D04](#d04), [D06](#d06), [U02](#u02), [U08](#u08)
- **출처:** [S-USER](#s-user), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** [DEC04](#dec04) (확정)

**완료 기준**

1. Goal/Plan/Task는 VM 공개 명령으로 생성·변경하고 별도 DAG 엔진을 만들지 않는다.
2. 권한 범위 안의 준비된 작업을 요청하고 blocked/unknown/validation 결과에 따라 다음 행동을 결정한다.
3. 모델의 응답 종료와 Goal 완료를 구분하며 VM verdict/evidence를 근거로 보고한다.

**기록된 증거**

기록 없음.

### AIK-027

**총괄 문맥·지시·작업 연속성**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Director continuity |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E04 · 총괄 에이전트 |
| 실행 순서 | 29 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

총괄 대화와 설정 revision을 영속 보존하고 VM/AGS 원장 참조로 작업 문맥을 복원한다.

- **산출물:** director context/checkpoint·지시 참조·handoff
- **검증 방법:** context 축약·재기동 후 동일 목표 복원 검증
- **선행 작업:** [AIK-025](#aik-025), [AIK-026](#aik-026)
- **요구사항:** [D03](#d03), [D04](#d04), [U02](#u02)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** 없음

**완료 기준**

1. 사용자 원문 지시와 후속 정정의 순서·적용 범위를 보존한다.
2. 문맥을 줄여도 미완료 목표·허용 범위·검증 근거는 권위 원장 참조로 회수한다.
3. 재개 시 새 Goal/Attempt를 무조건 만들지 않고 기존 ID·revision으로 이어간다.

**기록된 증거**

기록 없음.

### AIK-028

**총괄 모델 교체·허용 fallback·handoff**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Director |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E04 · 총괄 에이전트 |
| 실행 순서 | 30 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

모델을 바꾸거나 장애 시 허용된 대체 자원을 사용할 때 문맥·권한·실제 실행 이력을 보존한다.

- **산출물:** profile switch state machine·fallback policy
- **검증 방법:** 호출 중 변경·지원 도구 부족·인증 장애·handoff 검증
- **선행 작업:** [AIK-020](#aik-020), [AIK-023](#aik-023), [AIK-027](#aik-027)
- **요구사항:** [D02](#d02), [D03](#d03), [D05](#d05), [U02](#u02)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. 진행 중 호출 종료/취소와 새 binding 적용 시점을 정하고 같은 이벤트를 이중 처리하지 않는다.
2. 사용자가 설정한 대체 프로필과 품질·도구·데이터·예산 범위를 만족하는 경우만 전환한다.
3. 변경 이유·이전/새 모델·실제 effort·checkpoint를 기록하고 worker 프로필은 유지한다.

**기록된 증거**

기록 없음.

### AIK-040

**예산·쿼터·동시성 admission과 정산**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Admission and usage |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E07 · 관측·감사·비용 |
| 실행 순서 | 31 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

총괄과 worker 전체 사용량을 추적하고 신규 실행 전 한도를 예약한다.

- **산출물:** admission guard·usage reserve/settlement·quota status
- **검증 방법:** 경합 예약·429·잔여량 미확인·청구 지연 검증
- **선행 작업:** [AIK-005](#aik-005), [AIK-019](#aik-019), [AIK-020](#aik-020), [AIK-023](#aik-023)
- **요구사항:** [D02](#d02), [D05](#d05), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 총괄 호출을 포함해 자원/목표별 금액·토큰·시간·동시성 상한을 검사한다.
2. 구독 quota와 API 비용을 별도 단위로 보관하며 추정/확정/unknown을 구분한다.
3. 한도 도달 시 신규 배치를 멈추고 지연 usage를 정산한다. provider 외부 청구의 절대 상한을 보장한다고 표현하지 않는다.

**기록된 증거**

기록 없음.

### AIK-081

**Upstream A2 호환 AGS plugin 확보·적합성 통과**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Upstream AGS/VM coordination |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 32 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

기준선 VM 문서의 실제 gate 미개방 원인을 별도 upstream 선행 작업으로 추적한다. AIK에서 gate를 우회하지 않는다.

- **산출물:** 호환 AGS 설치 artifact·A2 conformance receipt·해결 변경 참조
- **검증 방법:** 실제 설치 plugin의 check-plugin·A2 계약 적합성 검사와 PASS receipt 확인
- **선행 작업:** [AIK-008](#aik-008)
- **요구사항:** [D06](#d06), [D08](#d08), [U08](#u08)
- **출처:** [S-AGS-ATTEST](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md) ([색인](#s-ags-attest)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC04](#dec04) (확정)

**완료 기준**

1. A2 same-user profile·manifest entrypoint·key/pin/digest 계약이 VM 기대와 맞는다.
2. 호환 배포물의 provenance와 conformance PASS 증거가 확보되기 전 완료 처리하지 않는다.
3. upstream 수정 필요 시 해당 저장소 변경/릴리스 참조를 남기고 AIK 구현 완료와 구분한다.

**기록된 증거**

기록 없음.

### AIK-082

**첫 출시 runtime의 모델·effort 관측 qualification**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Upstream runtime coordination |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 33 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

지원 matrix의 필수 API/호스트 경로에 실제 model/effort 관측과 AGS/VM 적합성 receipt를 확보한다. Codex 관측 gap과 Claude catalog/실측 차이는 경로별 준비 조건으로 추적한다.

- **산출물:** 지원 matrix revision/digest별 runtime/role attestation report·경로별 실제 receipt
- **검증 방법:** 지원 matrix의 API/구독 runtime별 설정값·실측 모델/effort·receipt 대조
- **선행 작업:** [AIK-008](#aik-008), [AIK-014](#aik-014), [AIK-015](#aik-015), [AIK-020](#aik-020)
- **요구사항:** [D02](#d02), [D06](#d06), [D08](#d08), [U02](#u02), [U08](#u08)
- **출처:** [S-AGS-ATTEST](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md) ([색인](#s-ags-attest)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. 동일 첫 출시 matrix의 필수 API/구독 runtime·역할 조합마다 실제 model/effort 관측과 receipt를 확보한다. adapter 구현·fixture 통과는 실제 qualification이 아니다. Codex GOVERNANCE_PROVIDER_UNSUPPORTED 등 필수 경로 미지원/NOT_RUN이 남으면 이 태스크는 완료할 수 없고 선택 경로 결과는 별도로 유지한다.
2. Claude configured catalog와 실제 provider 관측을 분리하고 API 경로를 포함한 각 필수 runtime이 해당 AGS/VM 관측 계약을 충족하는지 검증한다.
3. 미관측 필드를 추정으로 채우지 않는다. 비필수 미지원 경로는 qualified 경로와 상태를 분리하고, 필수 matrix를 변경하면 결정·영향·재검증 근거를 남긴다.

**기록된 증거**

기록 없음.

### AIK-032

**AGS 실행 전·완료 전 gate bridge**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | AGS bridge |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 34 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

설치형 AGS의 적합성과 실제 실행 관측을 VM의 필수 차단 조건으로 연결한다.

- **산출물:** VM 기존 governance gate·conformance 결과/cache·receipt를 소비하는 bridge와 상태 projection
- **검증 방법:** 호환 plugin과 manifest/관측 불일치의 설치형 검증
- **선행 작업:** [AIK-008](#aik-008), [AIK-031](#aik-031), [AIK-081](#aik-081), [AIK-082](#aik-082)
- **요구사항:** [D06](#d06), [D08](#d08), [U08](#u08)
- **출처:** [S-AGS-ATTEST](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md) ([색인](#s-ags-attest)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** [DEC04](#dec04) (확정), [DEC12](#dec12) (제안)

**완료 기준**

1. before_execution/before_completion gate를 모두 통과해야 governed 작업이 진행된다.
2. 필수 관측이 없거나 A2/manifest가 불일치하면 이유를 노출하고 gate를 생략하는 fallback이 없다.
3. AGS gate와 conformance 결과 재사용은 VM의 기존 공개 경로를 사용한다. AIK에 별도 판정/cache·Task 완료 권위를 만들지 않으며 AGS gate는 VM Core 판정의 추가 조건이다.

**기록된 증거**

기록 없음.

### AIK-030

**JEV RAW·metadata·AGENT 선택 계약 연결**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | JEV integration |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 35 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

사용자가 확정한 AGS/VM 내부 JEV 설계 원칙에 따라 실제 공개 입출력 계약·배포 상태를 확인하고 총괄/worker에 연결한다.

- **산출물:** JEV bridge·RAW/REQ provenance·agent choice 기록
- **검증 방법:** ON/OFF/실패·RAW 보존·최종 선택 차이 검증
- **선행 작업:** [AIK-001](#aik-001), [AIK-023](#aik-023), [AIK-031](#aik-031), [AIK-032](#aik-032)
- **요구사항:** [D02](#d02), [U08](#u08), [U09](#u09)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-JEV](#s-jev)
- **관련 결정:** [DEC05](#dec05) (확정)

**완료 기준**

1. JEV/대체 분류 경로의 실제 인터페이스·버전·배포 상태를 확인하고 미완료이면 upstream 변경 참조와 차단 사유를 기록한다. RAW를 보존하고 앞단 별도 LLM 정형화 없이 확인된 metadata만 공통 REQ에 담는다.
2. JEV 분류·평가 결과와 실제 AGENT의 최종 스킬 선택을 별도 기록한다.
3. OFF/호출 불가 시 해당 벤더 품질 기준을 통과한 최저비용 분류 후보 및 기존 운영 계약을 따르고 선택을 강제하지 않는다.

**기록된 증거**

기록 없음.

### AIK-033

**VM ready Task의 자원 placement**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Placement |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E06 · 배치·worker 수명 |
| 실행 순서 | 36 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

VM이 실행 가능하다고 제시한 Task를 AGS/AGENT 역할 요구와 실제 자원 능력에 맞게 배치한다.

- **산출물:** placement planner·eligibility filter·결정 근거
- **검증 방법:** 의존성·tool/model·capacity 조건별 배치 검증
- **선행 작업:** [AIK-019](#aik-019), [AIK-020](#aik-020), [AIK-023](#aik-023), [AIK-031](#aik-031), [AIK-032](#aik-032), [AIK-040](#aik-040)
- **요구사항:** [D04](#d04), [D05](#d05), [U06](#u06), [U08](#u08)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** [DEC04](#dec04) (확정)

**완료 기준**

1. VM의 ready/인가 판단을 입력으로 사용하고 별도 Task DAG/재시도 권위를 만들지 않는다.
2. AGS/실제 AGENT 및 VM 역할 계약이 요구한 모델·host·전문역량과 데이터 범위에 맞는 pool만 선택한다.
3. 독립 작업만 병렬 후보로 만들되 동일 프로젝트 Attempt는 VM의 직렬 기본을 유지한다. 해당 경로의 동시 실행 capability와 qualification 근거가 있을 때만 병렬 배치하고 이유·inventory revision을 기록한다.

**기록된 증거**

기록 없음.

### AIK-034

**worker lease·동시성 예약·fencing**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Lease manager |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E06 · 배치·worker 수명 |
| 실행 순서 | 37 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

기간과 작업 범위를 가진 사용권을 원자적으로 예약하고 중복 점유를 막는다.

- **산출물:** lease manager·reservation transaction·fence counter
- **검증 방법:** 동시 예약·TTL 만료·stale owner·중단 경합 검증
- **선행 작업:** [AIK-005](#aik-005), [AIK-033](#aik-033), [AIK-040](#aik-040)
- **요구사항:** [D04](#d04), [D05](#d05), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 한정된 pool capacity와 예산 예약을 경쟁 요청에서도 초과 점유하지 않는다.
2. lease는 VM task/attempt와 pool/worker를 참조하고 오래된 owner의 상태 갱신을 거절한다.
3. 만료와 실제 provider 종료를 구분하며 살아 있는 불명확 job은 즉시 재발사하지 않는다.

**기록된 증거**

기록 없음.

### AIK-035

**native·API 에이전트 실행과 도구 반복 연결**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Runtime dispatch |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E06 · 배치·worker 수명 |
| 실행 순서 | 38 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

VM RuntimeJob/Attempt를 native 호스트 또는 API agent loop로 실행한다. 모델의 도구 요청은 인가된 gateway를 통해 처리하고 결과를 다음 호출에 전달한다.

- **산출물:** native dispatch bridge·bounded API tool loop·provider job handle·receipt
- **검증 방법:** 전달 전/후 crash 및 중복 dispatch 검증
- **선행 작업:** [AIK-018](#aik-018), [AIK-029](#aik-029), [AIK-031](#aik-031), [AIK-032](#aik-032), [AIK-034](#aik-034)
- **요구사항:** [D04](#d04), [D06](#d06), [U06](#u06), [U08](#u08)
- **출처:** [S-CREDREAD](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreada) ([색인](#s-credread)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. request_id/contract digest/attempt/lease/provider job identity를 연결한다.
2. 예약 완료·전송 ACK·실제 실행 시작을 별도 상태로 남긴다.
3. native 경로는 기존 VM runtime을 재사용하고 관측된 실제 권한과 trusted same-user 여부를 기록한다. API 경로는 도구 요청→AIK-029 권한 gateway→VM/AGS 공개 command 또는 허용 도구→결과 반환→후속 모델 호출을 수행한다. 제한 도구 경로와 native OS 권한을 혼동하지 않으며 budget/중단·기존 Task/재시도 권위를 보존한다.

**기록된 증거**

기록 없음.

### AIK-036

**worker 실행 관측·스트림·usage 회수**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Runtime observer |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E06 · 배치·worker 수명 |
| 실행 순서 | 39 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

실제 job 상태, 결과, 모델/effort와 사용량을 adapter를 통해 관측한다.

- **산출물:** observer·event cursor·usage normalizer
- **검증 방법:** 스트림 단절·재접속·부분 결과·usage 누락 검증
- **선행 작업:** [AIK-035](#aik-035), [AIK-082](#aik-082)
- **요구사항:** [D02](#d02), [D04](#d04), [D06](#d06)
- **출처:** [S-AGS-ATTEST](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md) ([색인](#s-ags-attest)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. provider observed 모델/effort/job 상태와 요청 설정을 분리해 저장한다.
2. 이벤트 cursor gap과 중복을 감지하고 재조회로 복원한다.
3. usage 미제공과 실제 0을 구분하고 부분 출력이나 HTTP 성공만으로 완료 처리하지 않는다.

**기록된 증거**

기록 없음.

### AIK-037

**결과·artifact·evidence 수집과 digest 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Evidence relay |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E07 · 관측·감사·비용 |
| 실행 순서 | 40 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

worker 산출물을 접근 가능한 참조와 checksum으로 회수하고 VM/AGS 판정 근거에 연결한다.

- **산출물:** evidence relay·artifact manifest·checksum 검증
- **검증 방법:** 유실·변조·중복 callback·다른 workspace 경로 검증
- **선행 작업:** [AIK-017](#aik-017), [AIK-036](#aik-036)
- **요구사항:** [D01](#d01), [D06](#d06), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** 없음

**완료 기준**

1. artifact identity·SHA256·생성 attempt·접근 범위를 저장한다.
2. 같은 callback의 동일 결과는 dedup하고 다른 digest는 충돌로 처리한다.
3. 필수 근거 접근·무결성을 확인하고 권위 evidence identity/digest를 보존한다. 비밀 제거된 표시용 산출물은 별도 derivative identity/digest와 변환 provenance로 기록한다. 원본 보관이 정책상 불가하면 처음부터 비밀 없는 근거를 생산한다.

**기록된 증거**

기록 없음.

### AIK-038

**일시정지·취소·drain·자원 반환**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Worker lifecycle |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E06 · 배치·worker 수명 |
| 실행 순서 | 41 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

신규 배치를 멈추는 동작과 실제 provider job 취소·종료를 구분해 처리한다.

- **산출물:** cancel/drain/release controller
- **검증 방법:** 취소 직전 결과·지원 안 되는 취소·borrowed host 검증
- **선행 작업:** [AIK-034](#aik-034), [AIK-035](#aik-035), [AIK-036](#aik-036), [AIK-037](#aik-037)
- **요구사항:** [D03](#d03), [D04](#d04), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. 사용자 중단 즉시 신규 예약을 막고 실행 중 job은 adapter 지원 계약대로 처리한다.
2. 취소 요청과 취소 확인/늦은 성공을 구분하고 usage·evidence를 최종 회수한다.
3. 자체 생성 worker만 종료하며 빌려 쓰는 기존 호스트는 사용권을 반환한다.

**기록된 증거**

기록 없음.

### AIK-039

**재시작·unknown 결과·재시도 reconciliation**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Recovery |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E06 · 배치·worker 수명 |
| 실행 순서 | 42 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

앱·sidecar·네트워크 장애 후 provider identity를 먼저 조회하여 중복 실행을 방지한다.

- **산출물:** recovery reconciler·unknown resolution·retry bridge
- **검증 방법:** dispatch 경계 crash·timeout·늦은 callback·재부팅 검증
- **선행 작업:** [AIK-006](#aik-006), [AIK-035](#aik-035), [AIK-036](#aik-036), [AIK-038](#aik-038)
- **요구사항:** [D03](#d03), [D04](#d04), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** 없음

**완료 기준**

1. 저장된 attempt/lease/provider job을 VM 상태와 대조하고 실제 관측으로 회복한다.
2. 외부 효과가 불명확하면 무조건 재실행하지 않고 VM의 unknown/retry 계약을 따른다.
3. recover/retry/cancel 판단 이유·revision·원래 evidence를 덮지 않고 기록한다.

**기록된 증거**

기록 없음.

### AIK-041

**단일 클러스터 탄력 병렬화와 idle 정리**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Elasticity |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E06 · 배치·worker 수명 |
| 실행 순서 | 43 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:43:24Z` |

ready 작업과 검증된 용량에 맞춰 worker 수를 늘리고 유휴 사용권을 반환한다.

- **산출물:** local elasticity controller·backpressure
- **검증 방법:** 순차/제한 병렬·capacity 고갈·idle lease 검증
- **선행 작업:** [AIK-033](#aik-033), [AIK-034](#aik-034), [AIK-038](#aik-038), [AIK-040](#aik-040)
- **요구사항:** [D03](#d03), [D05](#d05), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VISION](#s-vision)
- **관련 결정:** 없음

**완료 기준**

1. VM의 실행 가능 상태와 프로젝트별 동시 실행 capability를 우선 적용한다. qualification을 통과한 범위에서 제약 내 worker를 확보하고 최소 한 지원 시나리오의 복수 worker 실제 동시 실행을 검증한다. 직렬 전용 경로의 제약은 유지한다.
2. 작업 완료/실패 뒤 evidence·usage를 회수하고 유휴 lease와 자체 생성 프로세스를 정리한다.
3. worker가 전 용량을 점유해 총괄·취소/관측 경로가 멈추지 않도록 제어용 여유를 보장한다.

**기록된 증거**

기록 없음.

### AIK-042

**AGS 독립검토와 VM 완료 판정 연결**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Validation bridge |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 44 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

작업자의 완료 선언과 실제 validation/audit/verdict를 연결해 근거 기반 완료를 만든다.

- **산출물:** validation/audit bridge·결과 요약 계약
- **검증 방법:** 실행 성공/근거 부족/감사 실패/통과 상태별 검증
- **선행 작업:** [AIK-029](#aik-029), [AIK-031](#aik-031), [AIK-032](#aik-032), [AIK-037](#aik-037)
- **요구사항:** [D04](#d04), [D06](#d06), [U08](#u08)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** [DEC04](#dec04) (확정)

**완료 기준**

1. provider success·ACK·validation·audit·GoalVerdict를 서로 다른 상태로 다룬다.
2. 필수 독립검토는 별도 역할·실행 provenance를 기록하고 자기 완료 선언을 감사 통과로 승격하지 않는다.
3. VM 최종 권위와 기존 인가 범위를 유지하며 후속 재작업 사유를 회수한다.

**기록된 증거**

기록 없음.

### AIK-043

**VM·AGS·AIK 이벤트 상호 참조와 조회 projection**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Observability |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E07 · 관측·감사·비용 |
| 실행 순서 | 45 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

GUI가 상태를 통합해서 읽도록 각 권위 원장을 ID·revision·cursor로 연결한다.

- **산출물:** read model·이벤트 재생·AGS session/worker 주소 매핑
- **검증 방법:** 중복·순서 역전·cursor gap·projection 재구축 검증
- **선행 작업:** [AIK-025](#aik-025), [AIK-031](#aik-031), [AIK-032](#aik-032), [AIK-037](#aik-037), [AIK-039](#aik-039)
- **요구사항:** [D04](#d04), [D06](#d06), [U03](#u03), [U08](#u08)
- **출처:** [S-AGS](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md) ([색인](#s-ags)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** 없음

**완료 기준**

1. VM Goal/Task/Attempt, AGS workflow/peer/session, AIK placement/lease를 연결한다.
2. 읽기 projection은 재생 가능하고 VM의 권위 원장으로 사용되지 않는다.
3. AGS 로컬 PEER의 전달/claim/ACK/result와 worker 주소를 연결하며 같은 사용자 범위를 보존한다.

**기록된 증거**

기록 없음.

### AIK-044

**두 벤더 단일 클러스터 실제 E2E**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Integration qualification |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E05 · AGS·VM 통합 |
| 실행 순서 | 46 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

GUI 연결 전에 한 Windows 논리 클러스터의 총괄→VM→AGS→두 벤더→근거→반환을 실증한다.

- **산출물:** 지원 matrix revision/digest별 local E2E·실측 report·설치형 identity
- **검증 방법:** 인가된 read-only Goal 1개·독립 Task 2개 실제 실행
- **선행 작업:** [AIK-022](#aik-022), [AIK-026](#aik-026), [AIK-027](#aik-027), [AIK-028](#aik-028), [AIK-030](#aik-030), [AIK-038](#aik-038), [AIK-039](#aik-039), [AIK-040](#aik-040), [AIK-041](#aik-041), [AIK-042](#aik-042), [AIK-043](#aik-043)
- **요구사항:** [D06](#d06), [D08](#d08), [U02](#u02), [U05](#u05), [U06](#u06), [U08](#u08)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. 총괄이 한 Goal을 받아 두 벤더의 qualified 자원에 독립 Task를 배치한다. 동일 지원 matrix의 필수 API/구독·총괄/worker 조합마다 허용 읽기 도구 요청→실행→결과 반환→후속 모델 응답을 실제 검증한다. 선택 경로의 미지원은 별도 기록하며 필수 경로 미지원/NOT_RUN은 통과하지 않는다. VM 직렬 제약과 qualified 경로의 동시성 조건을 보존한다.
2. 실제 모델/effort/job·usage·evidence·VM verdict와 AGS 검토 조건을 확인한다.
3. 재시작 후 동일 결과를 조회하고 lease 반환·중복 실행 없음까지 확인한다.

**기록된 증거**

기록 없음.

## M3 · Windows GUI 실제 연결

**범위:** 첫 출시 (`first_release`)

**단계 완료 조건:** 처음 연결부터 총괄 모델 지정·목표 실행·근거 확인·중단·복구까지 GUI로 수행한다.

### AIK-045

**Windows GUI shell·내비게이션·엔진 연결**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 47 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

기존 VM UI 자산을 검토해 실제 backend와 연결되는 데스크톱 shell을 만든다.

- **산출물:** GUI shell·connection indicator·route 구조
- **검증 방법:** 엔진 연결/재연결·버전 오류·화면 이동 검증
- **선행 작업:** [AIK-002](#aik-002), [AIK-004](#aik-004), [AIK-043](#aik-043), [AIK-044](#aik-044)
- **요구사항:** [U03](#u03), [U04](#u04), [U08](#u08)
- **출처:** [S-USER](#s-user), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** [DEC01](#dec01) (확정), [DEC07](#dec07) (미정)

**완료 기준**

1. 연결 관리·총괄·Goal·worker·근거·사용량·설정 화면으로 이동할 수 있다.
2. 실제 typed client/read model을 사용하고 mock 화면을 운영 상태로 표시하지 않는다.
3. 엔진 연결 불가/버전 불일치/오래된 상태를 사용자가 이해할 수 있게 표시한다.

**기록된 증거**

기록 없음.

### AIK-046

**첫 실행 온보딩·벤더 로그인 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 48 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

설치 후 연결 추가와 공식 로그인/API 키 등록을 터미널 명령 없이 수행하게 한다.

- **산출물:** onboarding·API/구독 연결 wizard
- **검증 방법:** 처음 사용하는 Windows 사용자의 전체 연결 흐름 검증
- **선행 작업:** [AIK-022](#aik-022), [AIK-045](#aik-045)
- **요구사항:** [U01](#u01), [U03](#u03), [U04](#u04)
- **출처:** [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. 벤더·계정·구독/API 실행 경로를 선택하고 공식 인증 흐름을 시작/완료한다.
2. API 키 입력은 마스킹하고 저장 후 실비밀을 화면 상태에 유지하지 않는다.
3. 연결 확인→총괄 모델 설정으로 이어지며 만료/취소/오류 후 재시도 지점을 표시한다.

**기록된 증거**

기록 없음.

### AIK-047

**시크릿 공간·계정·연결 관리 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 49 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

여러 벤더 연결을 한곳에서 확인·교체·해제하며 사용 중인 작업과의 관계를 보여준다.

- **산출물:** credentials/connections 관리 화면
- **검증 방법:** 다중 계정·이름 변경·키 회전·사용 중 삭제 GUI 검증
- **선행 작업:** [AIK-010](#aik-010), [AIK-016](#aik-016), [AIK-017](#aik-017), [AIK-018](#aik-018), [AIK-021](#aik-021), [AIK-045](#aik-045)
- **요구사항:** [D01](#d01), [U01](#u01), [U03](#u03)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** [DEC03](#dec03) (확정)

**완료 기준**

1. 계정 별칭·벤더·인증 유형·연결 상태·최종 확인 시각·사용 범위를 표시한다.
2. 연결 검사·키 교체·로그아웃·재인증·삭제를 GUI에서 수행한다.
3. 원문 비밀과 민감 오류는 표시하지 않고 잠금 상태와 진행 중 작업 영향을 보여준다.

**기록된 증거**

기록 없음.

### AIK-048

**자원·모델 capability 현황 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 50 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

장착된 실행 경로별 실제 지원 모델·도구·상태를 보여준다.

- **산출물:** resource inventory/detail 화면
- **검증 방법:** 인증 성공/실행불가·stale/unknown 상태 표시 검증
- **선행 작업:** [AIK-019](#aik-019), [AIK-020](#aik-020), [AIK-045](#aik-045)
- **요구사항:** [D02](#d02), [U03](#u03), [U05](#u05)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. 같은 모델의 구독/API 경로를 별도 자원으로 비교한다.
2. 실행·관측·취소·재연결·effort 지원과 검증 시각/출처를 표시한다.
3. 관측 불가 잔여량과 미지원 경로를 0이나 정상 상태로 표시하지 않는다.

**기록된 증거**

기록 없음.

### AIK-049

**총괄 모델·역할별 프로필 지정 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 51 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

총괄 실행 자원과 모델을 사용자가 지정하고 실제 적용 상태를 확인한다.

- **산출물:** director/role profile editor·적용 상태
- **검증 방법:** 다른 벤더 모델 선택·지원 옵션·실행 중 교체 GUI 검증
- **선행 작업:** [AIK-023](#aik-023), [AIK-024](#aik-024), [AIK-028](#aik-028), [AIK-045](#aik-045), [AIK-048](#aik-048)
- **요구사항:** [D02](#d02), [U02](#u02), [U03](#u03)
- **출처:** [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC02](#dec02) (확정)

**완료 기준**

1. 연결 자원→모델→지원 effort/도구→한도/대체 정책을 설정하고 총괄을 시작한다.
2. 총괄과 worker/검토 역할을 독립 설정하고 requested/actual applied를 구분해 보여준다.
3. 실행 중 변경은 checkpoint/교체 상태와 적용 이유를 보여주며 유효하지 않은 조합을 실행 전에 안내한다.

**기록된 증거**

기록 없음.

### AIK-050

**총괄 대화·목표·후속 지시 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 52 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

사용자가 목표와 추가 지시를 전달하고 총괄의 실제 진행·결과 보고를 읽는 주 화면을 만든다.

- **산출물:** director conversation·goal submission·진행 보고 UI
- **검증 방법:** 목표 제출·정정·재접속·실행 중단 GUI 시나리오
- **선행 작업:** [AIK-025](#aik-025), [AIK-026](#aik-026), [AIK-027](#aik-027), [AIK-029](#aik-029), [AIK-045](#aik-045), [AIK-049](#aik-049)
- **요구사항:** [D03](#d03), [D06](#d06), [U02](#u02), [U03](#u03)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. 원문 목표/후속 지시를 보존해 전달하고 해당 Goal/이벤트 ID로 연결한다.
2. 응답 스트림 종료와 작업 완료를 구분하며 목표의 현재 단계·필요한 사용자 입력을 표시한다.
3. 창을 다시 열면 같은 대화·목표·미처리 지시를 복원하고 중복 제출을 막는다.

**기록된 증거**

기록 없음.

### AIK-051

**Goal·Plan·Task 의존성과 상태 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 53 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

VM 권위 상태를 기반으로 작업 분해·진행·검증·대기 이유를 표시한다.

- **산출물:** goal/task list·dependency view·detail panel
- **검증 방법:** VM 상태/리비전 변경과 화면 상태 일치 검증
- **선행 작업:** [AIK-031](#aik-031), [AIK-043](#aik-043), [AIK-045](#aik-045), [AIK-050](#aik-050)
- **요구사항:** [D04](#d04), [D06](#d06), [U03](#u03), [U08](#u08)
- **출처:** [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core)), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** 없음

**완료 기준**

1. Goal/Plan revision/Task/Attempt와 선행 작업을 조회한다.
2. ready/running/blocked/unknown/result/verified 상태를 구분하고 담당·근거를 탐색한다.
3. 변경과 실행 명령은 VM façade를 통하며 GUI가 직접 완료 상태를 쓰지 않는다.

**기록된 증거**

기록 없음.

### AIK-052

**worker·세션 실시간 운영 현황판**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 54 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

실제 자원 점유와 어떤 모델이 어떤 일을 하는지 한 화면에서 확인한다.

- **산출물:** worker dashboard·session/task detail
- **검증 방법:** live event·연결 끊김·borrowed/owned worker 표시 검증
- **선행 작업:** [AIK-036](#aik-036), [AIK-038](#aik-038), [AIK-041](#aik-041), [AIK-043](#aik-043), [AIK-045](#aik-045)
- **요구사항:** [D02](#d02), [D03](#d03), [U03](#u03), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** 없음

**완료 기준**

1. worker 역할·벤더·host·요청/실제 모델·effort·Task·lease·마지막 갱신을 표시한다.
2. active/reserved/draining/released/unknown과 병렬도·사용량을 구분한다.
3. 개별/목표 단위 중단 요청과 실제 취소 확인을 별도 표시한다.

**기록된 증거**

기록 없음.

### AIK-053

**범위가 명확한 승인·추가 입력 화면**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 55 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

VM/AGS가 사용자 판단을 요구하는 경우 검토할 대상·효과·revision을 구체적으로 제시한다.

- **산출물:** approval/inbox GUI·stale 요청 처리
- **검증 방법:** 기존 승인 재사용·새 범위·중복/오래된 승인 검증
- **선행 작업:** [AIK-029](#aik-029), [AIK-042](#aik-042), [AIK-045](#aik-045), [AIK-051](#aik-051)
- **요구사항:** [D04](#d04), [D05](#d05), [U03](#u03)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** 없음

**완료 기준**

1. 기존 허용 범위를 다시 묻지 않고 새 판단이 필요한 작업만 노출한다.
2. 대상·변경/외부 효과·예산·입력 revision과 근거를 확인한 뒤 승인/거절한다.
3. 사용자 identity·승인 범위·시각을 기록하고 중복 클릭이나 stale approval을 새 권한으로 사용하지 않는다.

**기록된 증거**

기록 없음.

### AIK-054

**산출물·근거·감사·진단 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 56 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

Goal 결과와 실제 검증 근거, 비밀이 제거된 오류 이력을 탐색할 수 있게 한다.

- **산출물:** artifact/evidence viewer·audit history·support export
- **검증 방법:** 잘린/누락/변조 artifact·unsafe markup·sanitized export 검증
- **선행 작업:** [AIK-017](#aik-017), [AIK-037](#aik-037), [AIK-042](#aik-042), [AIK-043](#aik-043), [AIK-045](#aik-045)
- **요구사항:** [D01](#d01), [D06](#d06), [U03](#u03)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** 없음

**완료 기준**

1. VM verdict와 연결된 파일·digest·생성 attempt·검토 결과를 조회한다.
2. Markdown/HTML/링크 표시가 임의 스크립트·프로세스·파일 실행 권한을 획득하지 않는다.
3. 근거 접근 실패를 표시하고 지원 번들에 비밀 값이 포함되지 않는다.

**기록된 증거**

기록 없음.

### AIK-055

**총괄·worker 비용·quota·한도 GUI**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 57 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

실측 가능한 비용/사용량과 남은 실행 여유를 구분해서 보여준다.

- **산출물:** usage/cost dashboard·limit settings
- **검증 방법:** 구독/API·추정/확정·delayed usage 표시 검증
- **선행 작업:** [AIK-040](#aik-040), [AIK-043](#aik-043), [AIK-045](#aik-045), [AIK-052](#aik-052)
- **요구사항:** [D02](#d02), [D05](#d05), [U03](#u03)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 총괄과 worker의 일반/캐시/출력 토큰 및 비용을 지원되는 범위에서 분리한다.
2. 구독 quota를 API 금액으로 합산하지 않고 unknown·추정·확정과 갱신 시각을 표시한다.
3. 목표/자원 한도를 GUI에서 설정하고 신규 실행 중지 사유를 이해할 수 있게 보여준다.

**기록된 증거**

기록 없음.

### AIK-056

**Windows 트레이·재접속·한글·접근성**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Desktop GUI |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E08 · Windows GUI |
| 실행 순서 | 58 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

일상 운영에서 GUI 창의 수명과 총괄 실행을 구분하고 Windows 입력/표시 품질을 검증한다.

- **산출물:** tray/lifecycle UI·keyboard/a11y·Korean support
- **검증 방법:** 다중 DPI·한글 IME·키보드 이동·창 닫힘/재열기 검증
- **선행 작업:** [AIK-006](#aik-006), [AIK-027](#aik-027), [AIK-038](#aik-038), [AIK-039](#aik-039), [AIK-045](#aik-045), [AIK-050](#aik-050), [AIK-052](#aik-052)
- **요구사항:** [D03](#d03), [D07](#d07), [U03](#u03), [U04](#u04)
- **출처:** [S-USER](#s-user), [S-VM-GUI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md) ([색인](#s-vm-gui))
- **관련 결정:** 없음

**완료 기준**

1. 창 닫기·백그라운드 유지·일시정지·완전 종료가 정해진 정책과 표시대로 동작한다.
2. Windows 재접속 때 기존 인스턴스·목표를 복원하고 중복 총괄을 만들지 않는다.
3. 한글 경로/IME·키보드 탐색·가독성·지원 DPI에서 주요 흐름을 사용할 수 있다.

**기록된 증거**

기록 없음.

## M4 · Windows 첫 배포 검증

**범위:** 첫 출시 (`first_release`)

**단계 완료 조건:** 깨끗한 지원 Windows 환경의 설치본에서 실제 E2E·실패 복구·독립 검토를 통과해 배포한다.

### AIK-057

**Windows installer·runtime bundle·기동 구성**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Windows release |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 59 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

선택한 배포 형식으로 GUI와 검증된 엔진/adapter 의존성을 설치 가능하게 묶는다.

- **산출물:** Windows installer build·runtime manifest·hash
- **검증 방법:** 개발 도구 없는 지원 Windows 환경의 설치/제거 검증
- **선행 작업:** [AIK-003](#aik-003), [AIK-022](#aik-022), [AIK-044](#aik-044), [AIK-045](#aik-045), [AIK-046](#aik-046), [AIK-047](#aik-047), [AIK-048](#aik-048), [AIK-049](#aik-049), [AIK-050](#aik-050), [AIK-051](#aik-051), [AIK-052](#aik-052), [AIK-053](#aik-053), [AIK-054](#aik-054), [AIK-055](#aik-055), [AIK-056](#aik-056)
- **요구사항:** [D01](#d01), [D07](#d07), [U04](#u04)
- **출처:** [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC08](#dec08) (미정)

**완료 기준**

1. 필요한 GUI/엔진/runtime 의존성이 고정 provenance와 함께 제공된다.
2. 사용자별 설정/데이터 경로를 사용하고 비밀·개발 계정·운영 DB를 설치물에 넣지 않는다.
3. 설치·첫 실행·정상 제거가 동작하고 선택한 서명/무결성 정책을 적용한다.

**기록된 증거**

기록 없음.

### AIK-058

**업데이트·데이터 migration·복구·설정 백업**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Windows release |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 60 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

제품 업그레이드 실패에도 기존 설정·연결 참조·작업 이력을 복구할 수 있게 한다.

- **산출물:** update/rollback path·backup/restore·version check
- **검증 방법:** 중간 실패·구버전·새 Windows 사용자 복원 검증
- **선행 작업:** [AIK-005](#aik-005), [AIK-021](#aik-021), [AIK-039](#aik-039), [AIK-057](#aik-057)
- **요구사항:** [D01](#d01), [D03](#d03), [D07](#d07), [U04](#u04)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. AIK 데이터 migration 실패 시 원본을 유지하며 VM 원장 migration 정책을 임의로 변경하지 않는다.
2. 연결 metadata·프로필·작업 참조가 보존되고 해석 불가능한 비밀은 재인증 필요로 표시한다.
3. 업데이트 출처/무결성을 검증하고 실행 중 worker 처리·재개 정책을 적용한다.

**기록된 증거**

기록 없음.

### AIK-059

**Windows GUI 설치본 사용자 E2E**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Windows qualification |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 61 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

사용자 관점에서 인증·총괄 모델 지정·목표 실행·근거 회수·재시작을 실제 설치본으로 통과한다.

- **산출물:** 지원 matrix revision/digest 및 installer identity별 Windows GUI E2E·재현 기록
- **검증 방법:** 지원 matrix의 깨끗한 Windows 이미지에서 실사용 시나리오
- **선행 작업:** [AIK-057](#aik-057), [AIK-058](#aik-058)
- **요구사항:** [D06](#d06), [D08](#d08), [U01](#u01), [U02](#u02), [U03](#u03), [U04](#u04), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user)
- **관련 결정:** [DEC12](#dec12) (제안)

**완료 기준**

1. 터미널 명령 없이 두 벤더 연결→총괄 지정→역할별 worker→근거 조회를 완료한다. 동일 matrix의 필수 API/구독·총괄/worker 조합마다 실제 읽기 도구 실행과 후속 모델 응답을 확인한다. 선택 unsupported 경로는 정확히 안내하며 필수 경로 미지원/NOT_RUN은 이 태스크를 차단한다.
2. 요청/실제 model·effort와 VM/AGS 판정을 확인하고 실행 완료 뒤 lease가 회수된다.
3. 창 닫기/재열기와 재시작 후 상태·기존 목표·인증 연결을 복원한다.

**기록된 증거**

기록 없음.

### AIK-060

**실패·취소·중복·quota 장애 통합 검증**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Integration qualification |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 62 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

실제 실행 경계의 실패를 주입해 잘못된 성공과 중복 외부 효과가 생기지 않는지 검증한다.

- **산출물:** fault qualification matrix·evidence
- **검증 방법:** 프로세스 crash·API timeout/429·늦은 결과·인가 변경 시나리오
- **선행 작업:** [AIK-039](#aik-039), [AIK-040](#aik-040), [AIK-042](#aik-042), [AIK-057](#aik-057)
- **요구사항:** [D04](#d04), [D05](#d05), [D06](#d06), [D08](#d08), [U04](#u04)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. 전달 전/후 crash·callback 중복/역전·취소 경쟁·unknown 결과를 재현한다.
2. auth expiry·budget exhausted·capacity 고갈·stale input에서 신규 실행과 재시도를 통제한다.
3. 검증 실패/근거 부족을 완료로 표시하지 않고 회복 뒤 같은 외부 효과를 중복 실행하지 않는다.

**기록된 증거**

기록 없음.

### AIK-061

**Windows 시크릿·권한·출시 독립 검토**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Independent review |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 63 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

구현 담당과 구분된 검토 경로에서 인증·IPC·권한·실행 근거를 확인한다.

- **산출물:** independent audit report·수정 및 재검증 근거
- **검증 방법:** 설치본의 합성 비밀·broker 거부·OS 사용자/native 권한·증거 연결을 독립 검토
- **선행 작업:** [AIK-059](#aik-059), [AIK-060](#aik-060)
- **요구사항:** [D01](#d01), [D06](#d06), [D08](#d08), [U04](#u04)
- **출처:** [S-CREDREAD](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreada) ([색인](#s-credread)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** 없음

**완료 기준**

1. 합성 비밀로 vault·로그·prompt·IPC·support bundle의 노출을 점검하고 broker 범위 거부·다른 OS 사용자 접근·native 실행 principal과 실제 권한을 확인한다.
2. 결과 화면 콘텐츠/임의 IPC가 앱의 실행·시크릿 권한을 우회하지 못함을 검증한다. OS 격리를 광고한 경로는 실제 경계를 시험하며 trusted same-user 경로의 통과를 강한 프로세스 격리 PASS로 기록하지 않는다.
3. AGS/VM qualification과 AIK GUI 출시 판정을 분리하고 발견 결함의 해결 증거를 남긴다.

**기록된 증거**

기록 없음.

### AIK-062

**단일 클러스터 병렬성·응답성·사용량 실측**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Performance qualification |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 64 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:41:54Z` |

같은 성공 조건에서 순차/제한 병렬 실행과 GUI 운영 부담을 측정한다.

- **산출물:** performance/cost report·measurement fixture
- **검증 방법:** 동일 작업·모델·품질 기준의 비교 실험
- **선행 작업:** [AIK-059](#aik-059)
- **요구사항:** [D05](#d05), [D07](#d07), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. queue/startup/inference/gather/audit와 전체 완료 시간을 구분한다. VM 프로젝트별 직렬 제약을 유지하며 병렬 비교는 해당 capability qualification 근거가 있는 경로에서만 수행한다.
2. 총괄/worker 토큰·실제 비용·캐시 사용량을 지원 범위에서 측정한다.
3. 병렬 속도 향상과 비용 절감은 각각 실측 결과로 판정하고 GUI 정지/메모리 과다 기준을 확인한다.

**기록된 증거**

기록 없음.

### AIK-063

**제품 사용 안내·문제 해결·라이선스 정리**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Product documentation |
| 작업 유형 | 문서 (`documentation`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 65 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

GUI 사용자가 인증·총괄·작업·복구를 운영할 수 있는 안내와 배포 문서를 제공한다.

- **산출물:** in-app help·운영 가이드·license/third-party notices
- **검증 방법:** 설치본 화면·실제 support matrix와 문서 일치 검토
- **선행 작업:** [AIK-057](#aik-057), [AIK-059](#aik-059)
- **요구사항:** [D07](#d07), [U03](#u03), [U04](#u04)
- **출처:** [S-README](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/README.md) ([색인](#s-readme)), [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC10](#dec10) (미정), [DEC12](#dec12) (제안)

**완료 기준**

1. 연결·모델 지정·목표 실행·중단·재연결·오류 해결을 실제 화면 기준으로 설명한다.
2. 지원 matrix revision/digest에 따라 구현 상태와 실지원/미지원/NOT_RUN 경로, 확인 불가 usage, 데이터 보관/삭제 및 native same-user 신뢰 범위를 안내한다. 문서의 지원 주장은 실제 qualification 근거와 일치한다.
3. AIK 자체 라이선스를 확정하고 배포하는 upstream 구성요소의 notice를 포함한다.

**기록된 증거**

기록 없음.

### AIK-064

**Windows 첫 릴리스 최종 gate·배포**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P0 |
| 담당 컴포넌트 | Release lead |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E09 · Windows 출시 |
| 실행 순서 | 66 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

완료 근거를 하나의 설치본 identity에 묶어 첫 Windows 릴리스 판정을 내린다.

- **산출물:** release manifest·qualified installer·release notes
- **검증 방법:** 고정 installer hash/commit의 최종 qualification 검토
- **선행 작업:** [AIK-058](#aik-058), [AIK-059](#aik-059), [AIK-060](#aik-060), [AIK-061](#aik-061), [AIK-062](#aik-062), [AIK-063](#aik-063)
- **요구사항:** [D06](#d06), [D07](#d07), [D08](#d08), [U01](#u01), [U02](#u02), [U03](#u03), [U04](#u04), [U05](#u05), [U06](#u06), [U08](#u08)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-USER](#s-user), [S-VM](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md) ([색인](#s-vm))
- **관련 결정:** [DEC01](#dec01) (확정), [DEC06](#dec06) (제안), [DEC12](#dec12) (제안)

**완료 기준**

1. 같은 installer identity와 지원 matrix revision/digest에 두 벤더 실제 E2E·필수 auth/runtime/역할 경로의 live evidence·upstream conformance·복구·독립 검토를 연결한다.
2. 필수 경로 미지원/NOT_RUN 또는 다른 미해결 필수 조건이 있으면 출시 완료로 표시하지 않는다. 선택 경로 미지원은 구현 완료와 별개로 기록하고 실제 지원으로 광고하지 않는다. 필수 범위 변경은 결정·영향·재검증 근거 없이 gate를 완화할 수 없다.
3. 선택된 서명/무결성·지원 환경·알려진 제한과 배포물 hash를 포함해 출시한다.

**기록된 증거**

기록 없음.

## M5 · 탄력 운영·원격 PEER 연합

**범위:** 후속 개발 (`follow_up`)

**단계 완료 조건:** 두 물리적 환경에서 원격 입력 준비·위임·결과·취소·단절 복구·비용을 검증한다. 수신 측 입력 사전 복제를 성공의 전제로 삼지 않는다.

### AIK-065

**cache·rate limit·크레딧 기한 기반 배치 최적화**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Scheduling optimization |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E07 · 관측·감사·비용 |
| 실행 순서 | 67 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

관측된 비용과 유효기간에 따라 허용된 작업의 배치 전략을 최적화한다.

- **산출물:** measured scheduling policies·cache-aware queue
- **검증 방법:** 동일 품질 작업의 cache/rate-limit/기한 시나리오 비교
- **선행 작업:** [AIK-040](#aik-040), [AIK-041](#aik-041), [AIK-062](#aik-062), [AIK-064](#aik-064)
- **요구사항:** [D05](#d05), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VISION](#s-vision)
- **관련 결정:** 없음

**완료 기준**

1. provider/model/account/workspace/TTL별 cache 경계를 보존한다.
2. 실제 usage와 가격/credit 출처·시각에 따라 지연·비용·기한 정책을 적용한다.
3. 크레딧 소진 자체를 목표로 무관한 작업을 만들지 않고 기존 budget/품질 조건을 유지한다.

**기록된 증거**

기록 없음.

### AIK-066

**지원 Cloud worker 생성·재연결·종료 adapter**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Cloud adapters |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E03 · 자원·provider adapter |
| 실행 순서 | 68 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

공식 실행 경로가 제공하는 Cloud worker 수명주기를 조사하고 실제 지원 부분을 연결한다.

- **산출물:** Cloud lifecycle adapter·지원 capability matrix
- **검증 방법:** 공식 호스트 contract 확인과 제한된 실제 job 수명 검증
- **선행 작업:** [AIK-011](#aik-011), [AIK-035](#aik-035), [AIK-038](#aik-038), [AIK-039](#aik-039), [AIK-064](#aik-064)
- **요구사항:** [D02](#d02), [D04](#d04), [U05](#u05), [U06](#u06)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VISION](#s-vision)
- **관련 결정:** 없음

**완료 기준**

1. 생성/재연결/관측/취소/종료/usage 지원 범위를 실행 경로별로 확인한다.
2. 직접 생성 worker와 기존 세션 사용권을 구분해 lease/release한다.
3. 생성 API가 없는 경로는 unsupported로 표시하고 로컬 성공을 Cloud 성공으로 대체하지 않는다.

**기록된 증거**

기록 없음.

### AIK-067

**원격 PEER protocol·신뢰·권위 계약 결정**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation |
| 작업 유형 | 결정 (`decision`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 69 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

현재 AGS 로컬 PEER를 보존하면서 클러스터 간 신뢰·위임·상태 규약을 확정한다.

- **산출물:** versioned federation protocol ADR·message/input manifest schema·경로 projection 계약
- **검증 방법:** AGS/VM 계약 대조·위협/실패 모델 설계 검토
- **선행 작업:** [AIK-001](#aik-001), [AIK-043](#aik-043), [AIK-064](#aik-064)
- **요구사항:** [D04](#d04), [D05](#d05), [U07](#u07), [U08](#u08)
- **출처:** [S-AGS](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md) ([색인](#s-ags)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VISION](#s-vision)
- **관련 결정:** [DEC06](#dec06) (제안), [DEC11](#dec11) (미정)

**완료 기준**

1. hello/capability/offer/accept/result/cancel/lease 메시지와 schema 협상을 정의한다.
2. 상호 인증·짧은 작업 권한·key rotation/revoke·재전송/만료 조건을 정한다.
3. VM Goal 권위, 전역 cluster placement, 로컬 pool/lease의 소유자·복구 규칙을 명시한다. Task 입력 manifest의 논리 대상 ID·source revision·상대경로·내용 digest·허용 artifact 참조와 수신 경로 projection·입력 준비 책임을 정의한다.

**기록된 증거**

기록 없음.

### AIK-068

**클러스터 identity·pairing·상호 인증·회수**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation identity |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 70 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

독립 AIK 설치본을 peer로 등록하고 제한된 위임 권한을 부여·회수한다.

- **산출물:** peer registry·pairing handshake·trust controls
- **검증 방법:** 미등록/만료/회수 peer·key rotation 검증
- **선행 작업:** [AIK-009](#aik-009), [AIK-067](#aik-067)
- **요구사항:** [D01](#d01), [D05](#d05), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. cluster_id와 신뢰 identity를 연결하고 양쪽이 허용한 scope를 기록한다.
2. provider 자격증명 원문을 교환하지 않고 작업 위임 권한만 사용한다.
3. peer 회수/인증 만료 뒤 신규 위임을 중단하고 진행 중 실행의 관측/복구를 구분한다.

**기록된 증거**

기록 없음.

### AIK-069

**원격 PEER durable 전송·ACK·중복 처리**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation transport |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 71 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

네트워크를 넘는 message 전달·처리 ACK·결과 회신을 영속 큐로 연결한다.

- **산출물:** PEER gateway·inbox/outbox·재전송·dedup
- **검증 방법:** 단절·지연·역전·중복·replay·version mismatch 검증
- **선행 작업:** [AIK-025](#aik-025), [AIK-067](#aik-067), [AIK-068](#aik-068)
- **요구사항:** [D04](#d04), [U07](#u07), [U08](#u08)
- **출처:** [S-AGS](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md) ([색인](#s-ags)), [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. message/task/attempt/lease ID·contract digest·deadline을 보존한다.
2. 전송 성공·처리 ACK·실행 완료를 구분하고 ACK 손실을 재실행 권한으로 쓰지 않는다.
3. 재연결 시 cursor/sequence로 복구하며 만료·replay·호환불가 메시지를 거절한다.

**기록된 증거**

기록 없음.

### AIK-070

**클러스터 capability·가용 용량 광고**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation registry |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 72 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

원격 클러스터 내부 계정 정보를 노출하지 않고 배치에 필요한 능력만 광고한다.

- **산출물:** federated registry·TTL advertisement·health
- **검증 방법:** stale/offline/정책 변경·동시 capacity 예약 검증
- **선행 작업:** [AIK-019](#aik-019), [AIK-068](#aik-068), [AIK-069](#aik-069)
- **요구사항:** [D01](#d01), [D02](#d02), [D05](#d05), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 모델 능력·도구·검증 등급·용량·비용 힌트·데이터 범위와 갱신 시각을 교환한다.
2. 원문 secret·내부 계정·private filesystem 경로를 광고하지 않는다.
3. 오래된 광고를 배치 확정으로 신뢰하지 않고 수락 시 로컬에서 용량·정책을 재검사한다.

**기록된 증거**

기록 없음.

### AIK-071

**전역 클러스터 배치·입력 준비·하위 작업 위임**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation placement |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 73 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

상위는 실행 클러스터를, 하위는 자기 pool을 선택한다. 위임 입력을 수신 환경에 준비하고 원본 입력 계약과 결속하며 VM의 Goal/Task 권위는 보존한다.

- **산출물:** global placement·subtree contract·input materialization·delegation routing
- **검증 방법:** 다른 root의 입력 준비·digest/freshness·경로 탈출 거부·부분 수락·예산/권한·중복 위임 검증
- **선행 작업:** [AIK-033](#aik-033), [AIK-034](#aik-034), [AIK-040](#aik-040), [AIK-069](#aik-069), [AIK-070](#aik-070)
- **요구사항:** [D04](#d04), [D05](#d05), [U06](#u06), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VM-CORE](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md) ([색인](#s-vm-core))
- **관련 결정:** 없음

**완료 기준**

1. 상위 Goal/Task/contract revision·input manifest digest를 하위 lease/실행 identity에 결속한다. 허용 artifact만 수신 측 허용 root에 준비하고 bytes/digest/freshness를 실행 전에 검증한다. 원본 논리 대상과 수신 경로 projection을 구분하며 VM 권위를 복제하지 않는다.
2. 하위가 로컬 정책·capability·capacity와 입력 접근권한을 재검사한다. 상대경로 탈출·symlink 우회·누락·변조·stale 입력은 실행 전에 거절하고 구체 사유를 반환한다.
3. budget·권한·데이터 범위를 위임 시 축소할 수 있으나 임의 확대하거나 새 Task 권위를 만들지 않는다.

**기록된 증거**

기록 없음.

### AIK-072

**원격 결과·evidence·사용량 회수**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation evidence |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 74 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

클러스터 간 결과를 digest와 접근권한으로 회수하고 원래 VM/AGS 판정에 연결한다.

- **산출물:** remote evidence relay·usage settlement
- **검증 방법:** 원격 artifact 접근 실패/변조/중복·지연 usage 검증
- **선행 작업:** [AIK-037](#aik-037), [AIK-040](#aik-040), [AIK-042](#aik-042), [AIK-069](#aik-069), [AIK-071](#aik-071)
- **요구사항:** [D01](#d01), [D06](#d06), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 결과의 원격 attempt·lease·model provenance·artifact digest를 대조한다.
2. 원시 private 경로나 credential 원문을 전달하지 않고 범위가 제한된 artifact 접근을 사용한다.
3. 상위는 하위 완료 선언만으로 완료하지 않고 필요한 근거·검증/정산을 회수한다.

**기록된 증거**

기록 없음.

### AIK-073

**원격 lease fencing·partition·권위 회복**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation recovery |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 75 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

클러스터 단절과 coordinator 재시작에서 이중 배치·중복 외부 효과를 막는다.

- **산출물:** distributed recovery/fencing·cancel protocol
- **검증 방법:** 양쪽 재시작·partition·stale owner·늦은 성공 검증
- **선행 작업:** [AIK-034](#aik-034), [AIK-038](#aik-038), [AIK-039](#aik-039), [AIK-069](#aik-069), [AIK-071](#aik-071), [AIK-072](#aik-072)
- **요구사항:** [D03](#d03), [D04](#d04), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. fencing token과 실행 identity를 수신 측에서 검증한다.
2. lease 만료/peer offline과 provider job 종료를 구분하고 unknown 실행을 먼저 관측한다.
3. coordinator 회복·취소 경합·재위임에서 단일 권위와 기존 evidence를 유지한다.

**기록된 증거**

기록 없음.

### AIK-074

**두 클러스터 실제 연합 E2E·장애 qualification**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation qualification |
| 작업 유형 | 검증 (`validation`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 76 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T06:02:41Z` |

서로 다른 실행 환경에 설치된 두 AIK 클러스터가 한 Goal을 분담하도록 실증한다.

- **산출물:** federation qualification·peer identity·evidence bundle
- **검증 방법:** 송신 측에만 있는 파일/선행 산출물을 수신 측 빈 workspace·다른 root로 전달한 실제 네트워크 읽기 Task 및 실패 검증
- **선행 작업:** [AIK-068](#aik-068), [AIK-069](#aik-069), [AIK-070](#aik-070), [AIK-071](#aik-071), [AIK-072](#aik-072), [AIK-073](#aik-073)
- **요구사항:** [D06](#d06), [D08](#d08), [U06](#u06), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VISION](#s-vision)
- **관련 결정:** 없음

**완료 기준**

1. 각 클러스터가 자기 credential로 실행하며 offer/accept/ACK/result/audit를 구분한다. 송신 측에만 있는 파일과 선행 산출물을 빈/다른 root에 준비한 뒤 수신 bytes/digest·Task 입력 binding을 확인하고 실제 읽기 Task를 실행한다.
2. 단절·중복/역전 callback·취소·재시작 후 결과와 권위를 회복한다. 누락·변조·stale·접근불가 입력은 실행/완료를 차단하며 재조회로 사유와 입력 identity를 확인한다.
3. 양측 실제 모델·usage·근거·lease 반환을 확인하고 로컬 mock을 원격 성공으로 기록하지 않는다.

**기록된 증거**

기록 없음.

### AIK-075

**연합 비용·성능·운영 화면과 가이드**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P1 |
| 담당 컴포넌트 | Federation product |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E10 · 클러스터 연합 |
| 실행 순서 | 77 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

단일/다중 클러스터 운영의 차이를 실측하고 GUI에 peer 상태·위임·복구를 노출한다.

- **산출물:** federation GUI·benchmark·운영 가이드
- **검증 방법:** 동일 과제 단일/다중 cluster 비교와 peer 관리 흐름 검증
- **선행 작업:** [AIK-054](#aik-054), [AIK-055](#aik-055), [AIK-065](#aik-065), [AIK-074](#aik-074)
- **요구사항:** [D05](#d05), [D07](#d07), [U03](#u03), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. GUI에서 peer 연결/회수·capacity·위임 상태·원격 근거·unknown 사유를 조회한다.
2. 전송/queue/startup/실행/gather/감사 비용과 시간을 분리 측정한다.
3. 성공 범위·실패 복구·신뢰/데이터 범위와 측정 조건을 문서화한다.

**기록된 증거**

기록 없음.

## M6 · 추가 자원·조직·배포 확장

**범위:** 향후 확장 (`future`)

**단계 완료 조건:** 로컬 모델과 추가 제공자·조직 경계·선택적 Kubernetes 실행환경을 검증해 확장한다.

### AIK-076

**로컬 LLM 자원 adapter·GUI 장착**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P2 |
| 담당 컴포넌트 | Local adapters |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E11 · 후속 확장 |
| 실행 순서 | 78 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

로컬 모델 서버를 계정 비밀 없는 또는 별도 인증 자원으로 연결한다.

- **산출물:** local inference adapter·resource UI·qualification
- **검증 방법:** 지원 로컬 endpoint의 실행/취소/usage/한도 검증
- **선행 작업:** [AIK-011](#aik-011), [AIK-020](#aik-020), [AIK-048](#aik-048), [AIK-064](#aik-064)
- **요구사항:** [D05](#d05), [D06](#d06), [U03](#u03), [U05](#u05)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VISION](#s-vision)
- **관련 결정:** 없음

**완료 기준**

1. endpoint·모델·tool/effort 지원을 검증해 inventory에 등록한다.
2. 로컬이라는 이유로 도구 권한·품질·VM/AGS gate를 생략하지 않는다.
3. GUI에서 장착→역할 지정→작업→근거 회수를 수행하고 GPU/메모리 한도를 설정한다.

**기록된 증거**

기록 없음.

### AIK-077

**추가 벤더 adapter SDK·적합성 suite**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P2 |
| 담당 컴포넌트 | Adapter SDK |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E11 · 후속 확장 |
| 실행 순서 | 79 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

새 벤더가 같은 인증·실행·관측·취소 계약으로 편입될 수 있도록 확장 규약을 제공한다.

- **산출물:** adapter SDK·conformance kit·추가 vendor example
- **검증 방법:** 새 provider의 credential/model/job/error/usage 계약 검증
- **선행 작업:** [AIK-011](#aik-011), [AIK-020](#aik-020), [AIK-044](#aik-044), [AIK-064](#aik-064)
- **요구사항:** [D02](#d02), [D08](#d08), [U05](#u05), [U08](#u08)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-VISION](#s-vision)
- **관련 결정:** 없음

**완료 기준**

1. 벤더별 모델 이름·effort·인증을 공통 contract에 매핑하되 차이를 보존한다.
2. conformance를 통과한 capability만 실행 가능한 inventory로 광고한다.
3. 최소 한 추가 자원의 GUI 등록과 실제 governed E2E 근거를 확보한다.

**기록된 증거**

기록 없음.

### AIK-078

**역할·비용·품질 배치 정책 템플릿**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P2 |
| 담당 컴포넌트 | Policy templates |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E11 · 후속 확장 |
| 실행 순서 | 80 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

반복 운영의 총괄/worker 역할과 배치 조건을 재사용 가능한 검증된 설정으로 만든다.

- **산출물:** policy templates·schema/version·GUI 적용
- **검증 방법:** 정책 변경 전후 동일 목표·권한·품질 비교 검증
- **선행 작업:** [AIK-023](#aik-023), [AIK-028](#aik-028), [AIK-030](#aik-030), [AIK-065](#aik-065), [AIK-075](#aik-075)
- **요구사항:** [D05](#d05), [U02](#u02), [U06](#u06), [U09](#u09)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design)), [S-JEV](#s-jev), [S-VISION](#s-vision)
- **관련 결정:** 없음

**완료 기준**

1. 총괄/개발/검토 프로필과 최소 품질·budget·fallback 조건을 표현한다.
2. 설정 적용은 preview와 revision을 남기고 기존 Goal 인가 범위를 임의 확대하지 않는다.
3. JEV 분류 보조와 실제 AGENT의 최종 스킬 선택은 그대로 유지한다.

**기록된 증거**

기록 없음.

### AIK-079

**조직·workspace 간 클러스터 위임 권한**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P2 |
| 담당 컴포넌트 | Federation authorization |
| 작업 유형 | 기능 개발 (`feature`) |
| 에픽 | E11 · 후속 확장 |
| 실행 순서 | 81 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

서로 다른 운영 주체가 관리하는 클러스터의 데이터·작업·감사 범위를 분리한다.

- **산출물:** tenant/delegation policy·audit access·운영 문서
- **검증 방법:** 별도 조직 identity·권한 회수·artifact 접근 검증
- **선행 작업:** [AIK-021](#aik-021), [AIK-068](#aik-068), [AIK-071](#aik-071), [AIK-073](#aik-073), [AIK-074](#aik-074)
- **요구사항:** [D01](#d01), [D04](#d04), [D05](#d05), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. 사람/agent/cluster identity와 허용 위임 범위를 구분한다.
2. 조직 간 provider secret을 공유하지 않고 작업 계약·capability만 교환한다.
3. 데이터 등급·증거 열람·회수·동시성/예산 경계를 양측에서 검증한다.

**기록된 증거**

기록 없음.

### AIK-080

**선택적 실제 Kubernetes 실행환경 adapter**

| 항목 | 내용 |
|---|---|
| 상태 | 미착수 (`todo`) |
| 우선순위 | P2 |
| 담당 컴포넌트 | Deployment adapters |
| 작업 유형 | 통합 (`integration`) |
| 에픽 | E11 · 후속 확장 |
| 실행 순서 | 82 |
| 작성 / 수정 | `2026-10-12T05:36:30Z` / `2026-10-12T05:36:30Z` |

제품의 논리적 worker 계약을 실제 Kubernetes 배포 자원에 연결할 필요가 있을 때 추가한다.

- **산출물:** optional deployment adapter·별도 support contract
- **검증 방법:** 테스트 cluster의 생성/관측/drain/회수 검증
- **선행 작업:** [AIK-066](#aik-066), [AIK-071](#aik-071), [AIK-074](#aik-074)
- **요구사항:** [D04](#d04), [U06](#u06), [U07](#u07)
- **출처:** [S-DESIGN](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md) ([색인](#s-design))
- **관련 결정:** 없음

**완료 기준**

1. AIK의 이름을 Kubernetes API 호환 주장으로 사용하지 않고 연결 범위를 명시한다.
2. 실제 배포 자원과 worker/lease identity를 연결하며 기존 VM/AGS 권위와 인증 경계를 보존한다.
3. Windows GUI 첫 출시 의존성에 이 adapter나 Kubernetes 운영을 추가하지 않는다.

**기록된 증거**

기록 없음.

## 요구사항 색인

### D01

**비밀 값과 비밀 참조 분리**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-004](#aik-004), [AIK-005](#aik-005), [AIK-007](#aik-007), [AIK-009](#aik-009), [AIK-010](#aik-010), [AIK-016](#aik-016), [AIK-017](#aik-017), [AIK-018](#aik-018), [AIK-021](#aik-021), [AIK-022](#aik-022), [AIK-037](#aik-037), [AIK-047](#aik-047), [AIK-054](#aik-054), [AIK-057](#aik-057), [AIK-058](#aik-058), [AIK-061](#aik-061), [AIK-068](#aik-068), [AIK-070](#aik-070), [AIK-072](#aik-072), [AIK-079](#aik-079)

실비밀은 Windows 사용자 vault 또는 공식 호스트 저장소에 둔다. 프롬프트·장부·PEER·로그에는 참조와 비민감 metadata만 사용한다.

### D02

**설정값·실제 관측값·역할 분리**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-011](#aik-011), [AIK-012](#aik-012), [AIK-013](#aik-013), [AIK-015](#aik-015), [AIK-019](#aik-019), [AIK-020](#aik-020), [AIK-023](#aik-023), [AIK-024](#aik-024), [AIK-028](#aik-028), [AIK-040](#aik-040), [AIK-082](#aik-082), [AIK-030](#aik-030), [AIK-036](#aik-036), [AIK-048](#aik-048), [AIK-049](#aik-049), [AIK-052](#aik-052), [AIK-055](#aik-055), [AIK-066](#aik-066), [AIK-070](#aik-070), [AIK-077](#aik-077)

총괄과 worker 프로필을 분리하고 requested/provider_observed/configured_catalog를 혼동하지 않는다.

### D03

**총괄 지속성과 GUI 수명 분리**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-005](#aik-005), [AIK-006](#aik-006), [AIK-024](#aik-024), [AIK-025](#aik-025), [AIK-026](#aik-026), [AIK-027](#aik-027), [AIK-028](#aik-028), [AIK-038](#aik-038), [AIK-039](#aik-039), [AIK-041](#aik-041), [AIK-050](#aik-050), [AIK-052](#aik-052), [AIK-056](#aik-056), [AIK-058](#aik-058), [AIK-073](#aik-073)

이벤트 큐·동일 인스턴스 보장·재시작 회복으로 창을 다시 열어도 기존 목표와 실행을 이어간다.

### D04

**실행 권위와 중복 방지**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-001](#aik-001), [AIK-004](#aik-004), [AIK-005](#aik-005), [AIK-006](#aik-006), [AIK-016](#aik-016), [AIK-025](#aik-025), [AIK-031](#aik-031), [AIK-029](#aik-029), [AIK-026](#aik-026), [AIK-027](#aik-027), [AIK-033](#aik-033), [AIK-034](#aik-034), [AIK-035](#aik-035), [AIK-036](#aik-036), [AIK-038](#aik-038), [AIK-039](#aik-039), [AIK-042](#aik-042), [AIK-043](#aik-043), [AIK-051](#aik-051), [AIK-053](#aik-053), [AIK-060](#aik-060), [AIK-066](#aik-066), [AIK-067](#aik-067), [AIK-069](#aik-069), [AIK-071](#aik-071), [AIK-073](#aik-073), [AIK-079](#aik-079), [AIK-080](#aik-080)

VM Goal/Task/Attempt 권위와 AIK lease/placement 소유권을 유지하고 불명확한 실행의 자동 중복 발사를 막는다.

### D05

**자원·예산·쿼터·권한에 따른 배치**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-007](#aik-007), [AIK-018](#aik-018), [AIK-019](#aik-019), [AIK-021](#aik-021), [AIK-023](#aik-023), [AIK-029](#aik-029), [AIK-028](#aik-028), [AIK-040](#aik-040), [AIK-033](#aik-033), [AIK-034](#aik-034), [AIK-041](#aik-041), [AIK-053](#aik-053), [AIK-055](#aik-055), [AIK-060](#aik-060), [AIK-062](#aik-062), [AIK-065](#aik-065), [AIK-067](#aik-067), [AIK-068](#aik-068), [AIK-070](#aik-070), [AIK-071](#aik-071), [AIK-075](#aik-075), [AIK-076](#aik-076), [AIK-078](#aik-078), [AIK-079](#aik-079)

실행 capability, 인가, 동시성, 예산, 데이터 경계를 확인하고 실제 사용량과 예약을 정산한다.

### D06

**근거에 따른 완료와 통합 검증**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-008](#aik-008), [AIK-022](#aik-022), [AIK-026](#aik-026), [AIK-081](#aik-081), [AIK-082](#aik-082), [AIK-032](#aik-032), [AIK-035](#aik-035), [AIK-036](#aik-036), [AIK-037](#aik-037), [AIK-042](#aik-042), [AIK-043](#aik-043), [AIK-044](#aik-044), [AIK-050](#aik-050), [AIK-051](#aik-051), [AIK-054](#aik-054), [AIK-059](#aik-059), [AIK-060](#aik-060), [AIK-061](#aik-061), [AIK-064](#aik-064), [AIK-072](#aik-072), [AIK-074](#aik-074), [AIK-076](#aik-076)

ACK·실행 성공·검증·GoalVerdict를 구분하고 실제 설치형 경로의 검증 근거로 완료한다.

### D07

**Windows 운영성과 배포 품질**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-002](#aik-002), [AIK-003](#aik-003), [AIK-056](#aik-056), [AIK-057](#aik-057), [AIK-058](#aik-058), [AIK-062](#aik-062), [AIK-063](#aik-063), [AIK-064](#aik-064), [AIK-075](#aik-075)

설치·업데이트·한글 UI·접근성·실패 복구·진단 및 동작 측정을 첫 출시 범위로 검증한다.

### D08

**AGS/VM 호환성과 미완료 gate 해소**

- 구분: 설계에서 도출 (`derived`)
- 개발 태스크: [AIK-001](#aik-001), [AIK-008](#aik-008), [AIK-011](#aik-011), [AIK-014](#aik-014), [AIK-022](#aik-022), [AIK-031](#aik-031), [AIK-081](#aik-081), [AIK-082](#aik-082), [AIK-032](#aik-032), [AIK-044](#aik-044), [AIK-059](#aik-059), [AIK-060](#aik-060), [AIK-061](#aik-061), [AIK-064](#aik-064), [AIK-074](#aik-074), [AIK-077](#aik-077)

현재 문서에 남은 A2 conformance·model/effort 관측·GUI mock 경계를 실제 배포물 검증으로 해소한다.

### U01

**멀티벤더 로그인 시크릿 공간**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-007](#aik-007), [AIK-009](#aik-009), [AIK-010](#aik-010), [AIK-012](#aik-012), [AIK-013](#aik-013), [AIK-014](#aik-014), [AIK-015](#aik-015), [AIK-016](#aik-016), [AIK-017](#aik-017), [AIK-018](#aik-018), [AIK-021](#aik-021), [AIK-022](#aik-022), [AIK-046](#aik-046), [AIK-047](#aik-047), [AIK-059](#aik-059), [AIK-064](#aik-064)

여러 벤더의 계정·구독 연결·API 키를 GUI의 한 공간에서 등록·확인·교체·해제한다.

### U02

**Dot 역할 총괄 에이전트의 모델 지정**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-006](#aik-006), [AIK-007](#aik-007), [AIK-020](#aik-020), [AIK-023](#aik-023), [AIK-024](#aik-024), [AIK-025](#aik-025), [AIK-029](#aik-029), [AIK-026](#aik-026), [AIK-027](#aik-027), [AIK-028](#aik-028), [AIK-082](#aik-082), [AIK-044](#aik-044), [AIK-049](#aik-049), [AIK-050](#aik-050), [AIK-059](#aik-059), [AIK-064](#aik-064), [AIK-078](#aik-078)

사용자가 총괄 역할에 사용할 모델과 실행 자원을 지정하고 해당 에이전트가 목표·위임·보고를 지속한다.

### U03

**GUI 제품**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-002](#aik-002), [AIK-004](#aik-004), [AIK-043](#aik-043), [AIK-045](#aik-045), [AIK-046](#aik-046), [AIK-047](#aik-047), [AIK-048](#aik-048), [AIK-049](#aik-049), [AIK-050](#aik-050), [AIK-051](#aik-051), [AIK-052](#aik-052), [AIK-053](#aik-053), [AIK-054](#aik-054), [AIK-055](#aik-055), [AIK-056](#aik-056), [AIK-059](#aik-059), [AIK-063](#aik-063), [AIK-064](#aik-064), [AIK-075](#aik-075), [AIK-076](#aik-076)

핵심 연결·총괄 설정·작업 운영·결과 확인을 GUI에서 수행한다.

### U04

**첫 출시 Windows 전용**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-002](#aik-002), [AIK-003](#aik-003), [AIK-009](#aik-009), [AIK-022](#aik-022), [AIK-045](#aik-045), [AIK-046](#aik-046), [AIK-056](#aik-056), [AIK-057](#aik-057), [AIK-058](#aik-058), [AIK-059](#aik-059), [AIK-060](#aik-060), [AIK-061](#aik-061), [AIK-063](#aik-063), [AIK-064](#aik-064)

첫 제품 배포와 완료조건은 Windows를 대상으로 한다. 다른 OS 출시를 선행조건으로 두지 않는다.

### U05

**구독·API·로컬을 장착형 자원으로 통합**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-010](#aik-010), [AIK-011](#aik-011), [AIK-012](#aik-012), [AIK-013](#aik-013), [AIK-014](#aik-014), [AIK-015](#aik-015), [AIK-019](#aik-019), [AIK-020](#aik-020), [AIK-044](#aik-044), [AIK-048](#aik-048), [AIK-064](#aik-064), [AIK-066](#aik-066), [AIK-076](#aik-076), [AIK-077](#aik-077)

구독/API/로컬 모델을 공통 ResourcePool로 다루며 실행 경로별 인증과 능력 차이는 adapter가 보존한다.

### U06

**필요할 때 확장하고 반환하는 worker**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-040](#aik-040), [AIK-033](#aik-033), [AIK-034](#aik-034), [AIK-035](#aik-035), [AIK-037](#aik-037), [AIK-038](#aik-038), [AIK-039](#aik-039), [AIK-041](#aik-041), [AIK-044](#aik-044), [AIK-052](#aik-052), [AIK-059](#aik-059), [AIK-062](#aik-062), [AIK-064](#aik-064), [AIK-065](#aik-065), [AIK-066](#aik-066), [AIK-071](#aik-071), [AIK-074](#aik-074), [AIK-078](#aik-078), [AIK-080](#aik-080)

독립 작업을 제약 내에서 병렬 실행하고 결과·증거·사용량을 회수한 뒤 자원을 반환한다.

### U07

**AI-Kubernetes 클러스터 간 PEER 연합**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-067](#aik-067), [AIK-068](#aik-068), [AIK-069](#aik-069), [AIK-070](#aik-070), [AIK-071](#aik-071), [AIK-072](#aik-072), [AIK-073](#aik-073), [AIK-074](#aik-074), [AIK-075](#aik-075), [AIK-079](#aik-079), [AIK-080](#aik-080)

독립 클러스터들이 자기 권한과 인증정보를 유지하면서 작업을 위임하고 결과를 회수한다.

### U08

**AGS와 VibeMarshal을 기반으로 구축**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-001](#aik-001), [AIK-002](#aik-002), [AIK-008](#aik-008), [AIK-014](#aik-014), [AIK-015](#aik-015), [AIK-031](#aik-031), [AIK-029](#aik-029), [AIK-026](#aik-026), [AIK-081](#aik-081), [AIK-082](#aik-082), [AIK-032](#aik-032), [AIK-030](#aik-030), [AIK-033](#aik-033), [AIK-035](#aik-035), [AIK-042](#aik-042), [AIK-043](#aik-043), [AIK-044](#aik-044), [AIK-045](#aik-045), [AIK-051](#aik-051), [AIK-064](#aik-064), [AIK-067](#aik-067), [AIK-069](#aik-069), [AIK-077](#aik-077)

기존 AGS 거버넌스·PEER와 VM 계획/실행/복구 권위를 재사용한다.

### U09

**JEV는 내부 보조 계층이며 최종 선택은 AGENT**

- 구분: 사용자 요구 (`user`)
- 개발 태스크: [AIK-030](#aik-030), [AIK-078](#aik-078)

RAW를 보존하고 앞단 별도 LLM 정형화 없이 확인된 metadata만 구조화한다. JEV가 스킬 선택을 강제하지 않는다.

## 결정 색인

### DEC01

**첫 제품은 Windows GUI**

- 상태: 확정 (`confirmed`)
- 출처: [S-USER](#s-user)
- 관련 태스크: [AIK-002](#aik-002), [AIK-003](#aik-003), [AIK-045](#aik-045), [AIK-064](#aik-064)

**결정 내용:** Windows 데스크톱 GUI로 첫 출시한다.

**이유:** 현재 사용자 요구 3번. Windows 세부 지원버전·CPU·installer는 별도 열린 결정.

### DEC02

**총괄 에이전트 모델을 사용자가 지정**

- 상태: 확정 (`confirmed`)
- 출처: [S-USER](#s-user)
- 관련 태스크: [AIK-023](#aik-023), [AIK-049](#aik-049)

**결정 내용:** 총괄 실행 자원과 모델을 설정할 수 있게 한다.

**이유:** 현재 사용자 요구 2번. 실제 profile 필드와 worker 역할 분리는 제품 설계로 구체화.

### DEC03

**멀티벤더 인증 관리 공간**

- 상태: 확정 (`confirmed`)
- 출처: [S-USER](#s-user)
- 관련 태스크: [AIK-009](#aik-009), [AIK-010](#aik-010), [AIK-047](#aik-047)

**결정 내용:** 여러 벤더 로그인·API 연결을 한곳에서 관리한다.

**이유:** 현재 사용자 요구 1번. 비밀 저장 backend 선택은 별도 ADR.

### DEC04

**VM·AGS 권위 경계 유지**

- 상태: 확정 (`confirmed`)
- 출처: [S-DESIGN](#s-design)
- 관련 태스크: [AIK-001](#aik-001), [AIK-004](#aik-004), [AIK-005](#aik-005), [AIK-008](#aik-008), [AIK-031](#aik-031), [AIK-029](#aik-029), [AIK-026](#aik-026), [AIK-081](#aik-081), [AIK-032](#aik-032), [AIK-033](#aik-033), [AIK-042](#aik-042)

**결정 내용:** VM은 Goal/Task/Attempt/verdict, AGS는 거버넌스/협업, AIK는 pool/placement/lease를 소유한다.

**이유:** 최초 설계 §3과 VM 권위 설계의 중복 원장 금지 원칙을 유지.

### DEC05

**JEV 보조와 AGENT 최종 선택**

- 상태: 확정 (`confirmed`)
- 출처: [S-JEV](#s-jev)
- 관련 태스크: [AIK-030](#aik-030)

**결정 내용:** RAW 보존·앞단 별도 LLM 정형화 없음·최종 스킬 선택 AGENT를 유지한다.

**이유:** 사용자 정정 사항과 최초 설계 §10.

### DEC06

**첫 Windows 단일 클러스터 이후 연합 확장**

- 상태: 제안 (`proposed`)
- 출처: [S-DESIGN](#s-design)
- 관련 태스크: [AIK-064](#aik-064), [AIK-067](#aik-067)

**결정 내용:** M0~M4는 Windows 첫 배포, M5는 두 클러스터 연합, M6는 확장으로 단계화한다.

**이유:** GUI/인증/설치형 gate를 먼저 실증하기 위한 개발 순서 제안. 사용자 확정 OS 요구와 구분하며 연합 목표를 제거하지 않음.

### DEC07

**GUI 구현 스택**

- 상태: 미정 (`open`)
- 출처: [S-VM-GUI](#s-vm-gui)
- 관련 태스크: [AIK-002](#aik-002), [AIK-004](#aik-004), [AIK-045](#aik-045)

**결정 내용:** VM의 Tauri 2+React/TypeScript+Python sidecar 제안을 우선 검토하고 Windows spike 후 ADR로 선택한다.

**이유:** 기존 UI 자산을 검토하되 AIK 스택으로 자동 확정하지 않음.

### DEC08

**Windows 지원 범위·설치 형식**

- 상태: 미정 (`open`)
- 출처: [S-USER](#s-user)
- 관련 태스크: [AIK-003](#aik-003), [AIK-057](#aik-057)

**결정 내용:** Windows 버전·CPU 아키텍처·설치/업데이트/서명 방식을 AIK-003에서 확정한다.

**이유:** 사용자는 Windows만 확정했으며 x64/ARM64나 MSI/MSIX/EXE는 지정하지 않음.

### DEC09

**Windows 시크릿 backend**

- 상태: 제안 (`proposed`)
- 출처: [S-DPAPI](#s-dpapi)
- 관련 태스크: [AIK-009](#aik-009)

**결정 내용:** 사용자 범위 Credential Manager 또는 DPAPI 보호 저장소를 검증해 선택한다.

**이유:** 비밀 참조 broker는 앱의 접근 범위를 통제한다. Windows 사용자 vault의 저장/사용자 경계와 trusted same-user native 프로세스의 직접 OS 접근 범위를 별도 명시하고 실제 보장만 검증한다.

### DEC10

**AI-Kubernetes 라이선스**

- 상태: 미정 (`open`)
- 출처: [S-README](#s-readme)
- 관련 태스크: [AIK-063](#aik-063)

**결정 내용:** AIK 자체 라이선스와 재배포 notice를 배포 전에 확정한다.

**이유:** 최초 README는 미정. upstream 라이선스를 AIK 전체에 자동 적용하지 않음.

### DEC11

**원격 PEER 전송과 신뢰 계약**

- 상태: 미정 (`open`)
- 출처: [S-AGS](#s-ags)
- 관련 태스크: [AIK-067](#aik-067)

**결정 내용:** 버전·상호 인증·키 회수·메시지 수명·권위 회복과 입력 manifest/수신 경로 projection·준비 책임을 연합 구현 전에 확정한다.

**이유:** 현재 AGS 동일 사용자 로컬 PEER를 원격 신뢰 도메인과 구분.

### DEC12

**첫 출시 provider/runtime·역할 지원 matrix**

- 상태: 제안 (`proposed`)
- 출처: [S-DESIGN](#s-design)
- 관련 태스크: [AIK-001](#aik-001), [AIK-008](#aik-008), [AIK-012](#aik-012), [AIK-013](#aik-013), [AIK-014](#aik-014), [AIK-015](#aik-015), [AIK-019](#aik-019), [AIK-022](#aik-022), [AIK-082](#aik-082), [AIK-032](#aik-032), [AIK-044](#aik-044), [AIK-059](#aik-059), [AIK-063](#aik-063), [AIK-064](#aik-064)

**결정 내용:** OpenAI/Anthropic API와 지원 Codex/Claude Code 구독 bridge를 초기 구현 대상으로 삼는다. 두 벤더 및 API/지원 구독·총괄/worker 도구 실행의 필수 조합을 AIK-001의 판본형 matrix에서 명시한다. 012~015의 adapter 구현 완료와 022/082/044/059의 라이브 qualification을 구분하고 064는 동일 matrix의 필수 경로를 모두 검증해야 완료한다.

**이유:** 초기 범위는 제안이며 사용자 확정 요구와 구분한다. 선택 경로의 미지원은 다른 qualified 경로를 막지 않지만 미구현 adapter를 완료 처리하거나 필수 경로를 임의 축소하지 않는다. 범위 변경 시 결정·영향·재검증 기록을 남긴다.

## 출처 색인

### S-AGS

**AGS README 기준선**

- 구분: 저장소 기준선 (`repository`)
- 기준 버전 / 날짜: `56fef8bd189377b80f4020f506e717ab292b260a`
- 원문: [https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/README.md)

README는 2.9.0 공개 준비와 동일 컴퓨터·동일 OS 사용자 기본 범위를 설명. 이번 작업에서 제품 코드를 실행하지 않음.

### S-AGS-ATTEST

**AGS host execution attestation 계약**

- 구분: 저장소 기준선 (`repository`)
- 기준 버전 / 날짜: `56fef8bd189377b80f4020f506e717ab292b260a`
- 원문: [https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md)

문서 확인. 실제 배포물 manifest와 runtime 관측 증거의 설치형 검증은 별도 태스크.

### S-CRED

**Microsoft Credential Manager CredWrite**

- 구분: 공식 문서 (`official_doc`)
- 기준 버전 / 날짜: `accessed 2026-10-12`
- 원문: [https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritea](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritea)

Windows 사용자 자격 증명 생성·교체 API 후보. 구체 vault backend는 ADR로 확정.

### S-CREDREAD

**Microsoft CredRead: 현재 로그온 token의 사용자 자격 증명 조회**

- 구분: 공식 문서 (`official_doc`)
- 기준 버전 / 날짜: `2026-10-12 조회`
- 원문: [https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreada](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreada)

Windows 사용자 저장소의 접근 경계에 관한 공식 문서. 앱 broker scope가 동일 사용자 native 프로세스의 OS API 접근을 자동으로 제한한다는 근거는 아니다.

### S-DESIGN

**AI-Kubernetes 최초 설계 v0.1**

- 구분: 저장소 기준선 (`repository`)
- 기준 버전 / 날짜: `0367932ce2fa9369a3420a4867283167ea319188`
- 원문: [https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/docs/initial-design.ko.md)

문서 확인. VM 권위 원장, AIK placement/lease, AGS 거버넌스 경계와 연합 비전을 유지.

### S-DPAPI

**Microsoft CryptProtectData**

- 구분: 공식 문서 (`official_doc`)
- 기준 버전 / 날짜: `accessed 2026-10-12`
- 원문: [https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)

Windows 사용자 범위 DPAPI 검토 근거. 머신 범위를 기본으로 선택하지 않음. 일반적인 동일 사용자/기기 경계와 예외를 구분.

### S-JEV

**사용자가 확정한 JEV 및 최종 AGENT 선택 원칙**

- 구분: 사용자 제공 (`user`)
- 기준 버전 / 날짜: `2026-10-08`
- 원문: 사용자 제공 대화

JEV는 AGS/VM 내부 보조 계층. RAW 보존, 앞단 별도 LLM 정형화 없음, 최종 스킬 선택은 실제 AGENT.

### S-README

**AI-Kubernetes 최초 README**

- 구분: 저장소 기준선 (`repository`)
- 기준 버전 / 날짜: `0367932ce2fa9369a3420a4867283167ea319188`
- 원문: [https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/README.md](https://github.com/jaeseongs95/AI-Kubernetes/blob/0367932ce2fa9369a3420a4867283167ea319188/README.md)

문서 확인. 저장소 전체 tree에는 README와 최초 설계만 존재; 제품 구현·검증 결과 없음.

### S-USER

**현재 사용자 요청: 인증 공간·총괄 모델·Windows GUI**

- 구분: 사용자 제공 (`user`)
- 기준 버전 / 날짜: `2026-10-12`
- 원문: 사용자 제공 대화

사용자가 개발 태스크를 SQLite로 정리하여 AI-Kubernetes에 push하도록 요청. 3개 추가 요구를 직접 확정.

### S-VISION

**첨부 대화의 AI-Kubernetes 사용자 확정 비전**

- 구분: 사용자 제공 (`user`)
- 기준 버전 / 날짜: `2026-10-12`
- 원문: 사용자 제공 대화

구독/API/로컬 자원 장착, 탄력 worker, AGS+VM, 클러스터 간 PEER. 이전 assistant의 비유나 구현 추정은 완료 근거로 사용하지 않음.

### S-VM

**VibeMarshal README 기준선**

- 구분: 저장소 기준선 (`repository`)
- 기준 버전 / 날짜: `32bb0f9dd9f024045d24487312b50f5b703573a3`
- 원문: [https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/README.md)

0.2.0a1; schema 4/RuntimeJob/façade 구현 명시, 1.0 NO-GO, GUI mock. A2 호환 gate와 Codex model/effort 관측에 미완료 조건 명시. 실행 미검증.

### S-VM-CORE

**VM orchestration 권위 설계**

- 구분: 저장소 기준선 (`repository`)
- 기준 버전 / 날짜: `32bb0f9dd9f024045d24487312b50f5b703573a3`
- 원문: [https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/orchestration-redesign.md)

Goal/Plan/Task/Attempt 및 verdict는 VM 권위. README와 일부 freeze/qualification 상태 서술 차이는 설치본 증거로 해결.

### S-VM-GUI

**VM GUI 인터페이스 설계**

- 구분: 저장소 기준선 (`repository`)
- 기준 버전 / 날짜: `32bb0f9dd9f024045d24487312b50f5b703573a3`
- 원문: [https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md)

Tauri 2+React/TypeScript+Python sidecar 제안과 비권위 EngineClient를 재사용 후보로 검토. schema2 예시와 현재 schema4 차이 확인 필요.

## DB 메타데이터

| 키 | 값 |
|---|---|
| `audit_baseline_commit` | 019cccf0bd63684d893f50d239d54fbc2bf7b033 |
| `baseline_commit` | 0367932ce2fa9369a3420a4867283167ea319188 |
| `canonical_data` | planning/backlog.sqlite3 |
| `created_at` | 2026-10-12T05:36:30Z |
| `evidence_policy` | Repository contents were read; no upstream product code or Windows product tests were executed. |
| `language` | ko |
| `ownership_policy` | VM owns Goal/Task/Attempt/verdict; AGS governance/peer; AIK owns pool/placement/lease. |
| `product_status` | pre_implementation |
| `project` | AI-Kubernetes |
| `review_snapshot` | planning/backlog.sql |
| `reviewed_at` | 2026-10-12T06:02:41Z |
| `schema_version` | 1 |
| `scope_policy` | Windows GUI confirmed; local-first release staging proposed in DEC06; federation retained in M5. |
| `secret_policy` | Development metadata only; runtime credentials and operational ledgers never belong in this database. |
| `task_status_policy` | All product tasks remain todo; planning is not implementation evidence. in_progress/done require all prerequisites done; done also requires reviewed evidence. Adapter implementation and route qualification are distinct. |
| `updated_at` | 2026-10-12T06:02:41Z |
| `upstream_ags_commit` | 56fef8bd189377b80f4020f506e717ab292b260a |
| `upstream_vm_commit` | 32bb0f9dd9f024045d24487312b50f5b703573a3 |
| `user_request_date` | 2026-10-12 |
