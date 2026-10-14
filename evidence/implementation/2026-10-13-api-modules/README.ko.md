# 부분 구현 기록 — 2026-10-13 모듈 구현

이 기록은 [개발 태스크 정본](../../../planning/backlog.sqlite3)에 부분 구현·검증 범위·남은 완료 기준·차단 사유를 반영한 근거다. **어느 태스크도 완료(`done`)로 처리하지 않았다.** 상태는 `todo`에서 `blocked`로만 바꿨다. 선행 작업(특히 AIK-003·004·005·011·083)이 완료되지 않았고, 실제 Windows·provider·VM/AGS 설치형 검증이 실행되지 않았기 때문이다.

## 검증 범위와 한계

- 모든 시험은 표준 라이브러리 `unittest`와 합성 데이터·fake port만 쓴다. 이 세션의 Linux(Python 3.13)에서 `python -m unittest discover -s tests`로 전체 회귀를 실행해 통과했다(각 commit 직전).
- 독립 CI는 `.github/workflows/aik-modules.yml`(Ubuntu·Windows, Python 3.14.8)로 연결했다. CI 통과는 제품 qualification이 아니다.
  - run #23(`15940c5`, 2026-10-13): 모듈 시험 1172개가 Ubuntu와 windows-2022(Windows Server 2022)에서 모두 통과했다.
  - 같은 run에서 VM 32bb0f9를 git에서 설치한 실제 upstream 적합성 시험 29개가 두 OS 모두 통과했다(skip 0).
  - windows-2022는 Windows Server다. Windows 11 데스크톱, 사용자 세션, 다중 사용자 격리 검증은 아니다.
- 구현 초안과 독립 검토·수정은 Claude API(Opus) 호출로 만들었다. 적용·시험·판정·commit은 세션 총괄이 수행했다. 각 태스크는 독립 검토 finding을 반영한 수정 commit을 포함한다(일부는 검토 진행 중).
- VM 인터페이스 중 일부(실행 binding extension, 효과 intent/receipt port)는 현재 upstream에 없는 **가정 확장**이다. 실제 설치형 upstream에 연결하기 전에는 통합 완료로 처리하지 않는다.
- 실제 연결 근거: VM 어댑터의 고정 revision 소스 적합성 시험([vm-bridge 문서](../../../docs/vm-bridge.ko.md)), Anthropic Messages API 최소 진단([기록](../../provider-adapter/2026-10-13-anthropic-live-probe/README.ko.md)), Windows CI(`aik-modules.yml`)가 찾은 Windows 결함 수정(file ID 범위, shell 분류 순서).
- 각 태스크의 완료 기준 판정은 API가 장부 명세와 구현 문서를 대조해 만든 초안이다. 세션 총괄이 현재 코드와 대조해 일부를 정정했다. `단위 범위 충족`은 합성 시험 범위에서의 판단이며 완료 판정이 아니다.

## AIK-006

- 구현: src/aik_supervisor/에 사용자·workspace·component별 OS 파일 잠금, 원자적 endpoint 게시와 attach, 창·수명 상태 분리, 종료 시 진행 중 lease 보존 보고를 구현했다.
- 검증: Linux에서 합성 데이터로 실제 자식 프로세스 동시 기동·attach·kill 후 재기동, 일시정지/종료 배치 거절, mock 저장소 오류, msvcrt 분기를 단위 시험으로 검사했다.
- 커밋: `de78cdd`, `38b1e5a`
- 시험: `tests/test_supervisor.py` (16개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | 잠금과 endpoint attach는 단위 범위에서 동작한다. 그러나 named pipe/socket 서버·handshake와 Electron GUI 연결이 없다. |
| 2 | 단위 범위 충족 | 창 축과 수명 축(RUNNING/PAUSED/SHUTTING_DOWN/STOPPED)을 분리했다. 다만 일시정지는 메모리에만 있고 GUI 이벤트와 연결되지 않았다. |
| 3 | 부분 | 종료 시 배치 거절·lease 보존·재시작 observation_targets를 구현했다. 단, 거절은 admit_placement 경로에만 적용되고 스캔은 store 전체 대상이다. |

차단 사유:
- 선행 AIK-004(blocked)·AIK-005 장부상 미완료
- endpoint 서버·instance_id handshake 미구현으로 잔여 경쟁 창 존재
- Windows 공유 위반 시 PermissionError 미처리
- Electron GUI 실제 연결 없음

실행하지 않은 검증(NOT_RUN):
- Windows 11 msvcrt 잠금·LOCALAPPDATA·강제 종료 후 잠금 해제·NTFS 대소문자
- named pipe 서버·사용자별 ACL·재연결 handshake
- 다른 Windows 사용자·RDP 세션 격리

## AIK-007

- 구현: aik_config SettingsStore를 구현했다. workspace·연결·총괄/worker 프로필·한도·권한 기본값의 별도 모델, 참조 검증, revision, 비밀 제외 export/import, AIK-084 결속 authority 인터페이스를 포함한다.
- 검증: Linux 임시 폴더의 합성 데이터로 재시작 유지, 잘못된 참조·교차 workspace, revision 충돌·rollback, export/import, 결속 일치/불일치를 단위 시험으로 검사했다.
- 커밋: `df8af3d`, `ab61cb3`
- 시험: `tests/test_config.py` (20개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | workspace·연결·agent 프로필·한도·권한을 별도 테이블과 frozen dataclass로 구분 저장한다. 연결 상세는 AIK-010 범위다. |
| 2 | 단위 범위 충족 | InvalidReference·CrossWorkspaceReference를 검증 단계에서 발생시키고, composite FK로 다른 workspace 연결 참조를 차단한다. |
| 3 | 단위 범위 충족 | export에서 비밀·credential을 제외하고, import 시 needs_authentication과 connections_requiring_authentication을 표시한다. |
| 4 | 부분 | 결속 authority 인터페이스와 fail-closed 검증만 구현했다. AIK-084는 미구현이고 ExecutionTarget의 선택 변경 fencing이 없다. |

차단 사유:
- AIK-084 aik_workspace가 WorkspaceBindingAuthority를 구현했으나 AIK-084 자체가 미완료이고 GUI·실제 VM 결속 검증 없음
- 선행 AIK-004·AIK-005 장부상 미완료
- AIK-010 연결 CRUD·GUI 연결 미연동

실행하지 않은 검증(NOT_RUN):
- Windows 기본 경로·잠금·journal 사본 검사
- AIK-084 실제 구현과의 연동
- GUI·admission 연결

## AIK-008

- 구현: aik_compat.upstream_defects로 진단 결과를 결함 목록으로 변환한다. baseline 기대 identity·상태·결함 소유자, AIK-081/082 연결, 경로별 상태, GUI 사유와 저장 진단 재계산 결속을 포함한다.
- 검증: 저장소의 고정 계약 JSON과 메모리 내 변조 입력으로 결함 목록 생성과 위조·불일치 입력 거절을 Linux 단위 시험으로 검사했다.
- 커밋: `eb51d14`, `d3dae47`
- 시험: `tests/test_upstream_defects.py` (22개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | 상태·manifest·digest를 기록하지만 identity는 baseline 계약 기대값이다. 설치물 관측이 아니며 MATCH·compatible을 출력하지 않는다. |
| 2 | 부분 | linkage와 route_states로 경로별 상태를 분리 연결한다. 그러나 model/effort 관측은 live probe가 없어 NOT_RUN이다. |
| 3 | 단위 범위 충족 | PASS·qualified를 발행하지 않고, 실행불가 원인을 고정 코드·메시지 키로 gui.reasons에 반환한다. |

차단 사유:
- 선행 AIK-083 미완료
- 설치형 conformance 미실행
- A2 호환 AGS 배포물 부재
- AIK-081/082 live probe 근거 없음

실행하지 않은 검증(NOT_RUN):
- 실제 AGS/VM 설치형 conformance(--live)
- Windows 11 실기
- 벤더별 model/effort live probe와 AIK-086 governed qualification
- 실제 live 진단 파일 재계산 대조

## AIK-009

- 구현: 사용자 범위 DPAPI와 암호문 파일 backend, put/replace/delete/resolve, LOCAL_MACHINE 플래그 거절, 고정 오류 코드, 경계 ADR을 src/aik_vault/와 문서로 구현했다.
- 검증: FakeVaultBackend·mock binding으로 저장·교체·삭제·재시작 회수·손상·잠금·경로 거절·평문 비검출 단위 시험을 작성했다. 문서에는 실행 결과 기록이 없다.
- 커밋: `36f7e69`, `72a9d33`
- 시험: `tests/test_vault.py` (26개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | DPAPI 선택과 머신 범위 금지, 동일 사용자 접근 범위를 ADR에 명시했다. Credential Manager 실기 비교와 Windows 검증은 NOT_RUN이다. |
| 2 | 부분 | fake 백엔드로 CRUD와 프로세스 재시작 후 회수를 시험하도록 작성했다. 실제 DPAPI 왕복·재시작 시험은 Linux에서 skip된다. |
| 3 | 부분 | 시험 산출물 범위의 평문 비검출과 오류 코드 매핑을 작성했다. Windows ACL 거부 분류와 DPAPI 오류 매핑은 NOT_RUN이다. |

차단 사유:
- 실제 Windows DPAPI 실행 근거 부재
- 선행 AIK-004·AIK-005 장부상 미완료
- Credential Manager 정책 근거 자료 없음

실행하지 않은 검증(NOT_RUN):
- 실제 Windows DPAPI 왕복·프로세스 재시작 회수
- 다른 Windows 사용자·다른 PC 복호화
- LOCALAPPDATA·사용자 지정 디렉터리 ACL과 ACL 거부 분류
- Credential Manager 실기 비교

## AIK-010

- 구현: aik_connection ConnectionStore를 구현했다. 상세 metadata 별도 DB, 실행 경로별 규칙, 중복 판정, 인증 문맥 검증, label 제외 provenance, 삭제 영향 범위와 강제 삭제를 포함한다.
- 검증: OS 임시 폴더의 합성 데이터와 FakeVaultBackend로 다중 계정, 중복, 인증 문맥, 이름 변경, 삭제 영향 범위, 모의 부분 게시를 Linux 단위 시험으로 검사했다.
- 커밋: `ded0b64`, `9dd4dc2`
- 시험: `tests/test_connection.py` (27개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | provider·account label·auth_kind·execution_kind·credential_ref·verified_at을 연결 상세 DB에 저장한다. |
| 2 | 부분 | 구독/API 별도 등록, binding_revision, verify_auth_context 대조를 구현했다. 실제 공식 호스트의 AuthContext 관측 어댑터는 없다. |
| 3 | 단위 범위 충족 | rename 후에도 ConnectionProvenance가 불변이고 impact로 영향 범위를 표시한다. 외부 참조 검사는 원천을 주입해야 한다. |

차단 사유:
- 선행 AIK-007·AIK-009 장부상 미완료
- 공식 호스트 인증 문맥 관측 어댑터 미구현
- 외부 참조 조회와 삭제가 비원자적이며 tombstone 없음

실행하지 않은 검증(NOT_RUN):
- Windows 기본 경로·잠금·실제 DPAPI vault 연동
- 실제 디스크 full·I/O 오류·전원 차단
- 실제 공식 호스트 인증 관측

## AIK-012

- 구현: OpenAIApiAdapter를 구현했다. 키 등록·인증 확인·모델 확인·호출·관측·중단·교체·해제, 공통 turn/tool 계약 변환, 실행 가능 모델 광고 제한, 구현/qualification 상태 분리를 포함한다.
- 검증: 합성 fake transport와 FakeVaultBackend로 성공/실패 fixture·GuardedAdapter 결합·평문 비검출 단위 시험을 작성했다. 문서에는 실행 결과 기록이 없다.
- 커밋: `da6e014`, `6a80c64`
- 시험: `tests/test_adapter_openai.py` (32개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | 수명주기 코드 경로와 성공/실패 fixture 시험을 작성했다. 문서에 시험 실행 결과 기록이 없다. |
| 2 | 부분 | request/job·model·usage·도구 메시지를 공통 계약으로 변환한다. AIK-035 실제 결합은 없고 엔드포인트 형식은 미검증 가정이다. |
| 3 | 부분 | 검증된 모델만 광고하고 api_key_metered로 과금을 구분하며, status는 NOT_RUN으로 분리한다. 시험 실행 기록은 없다. |

차단 사유:
- 선행 AIK-009·AIK-011 장부상 미완료
- 엔드포인트·응답 형식이 vendor 문서로 미확인
- AIK-022/082/086 단계별 근거 없음

실행하지 않은 검증(NOT_RUN):
- UrllibTransport 실제 vendor 왕복
- 실제 키 인증(AIK-022)
- runtime·도구 실행(AIK-082/086/044)
- Windows DPAPI vault 결합 실행

## AIK-013

- 구현: AnthropicAdapter를 구현했다. 키 등록·인증 확인·Messages 호출·교체·해제, model·usage·cache·도구 변환, 잘림 비승격, 공통 오류 매핑, qualification NOT_RUN 분리를 포함한다.
- 검증: 합성 fake transport와 fake vault로 인증·호출·usage/cache·도구 loop·잘림·401/429/404/timeout·교체·해제 fixture 시험을 작성했다. 문서에는 실행 결과 기록이 없다.
- 커밋: `42c5a12`, `00ed825`, `8d9cf5c`
- 시험: `tests/test_adapter_anthropic.py` (42개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | 수명주기 코드 경로와 성공/실패 fixture 시험을 작성했다. 문서에 실행 결과 기록이 없다. |
| 2 | 부분 | usage·cache·도구 변환을 구현하고 max_tokens는 truncated/failed로 처리한다. 단, source 선언을 검증하지 못하고 AIK-035 실제 결합이 없다. |
| 3 | 부분 | 401/429/timeout/404를 공통 FaultKind로 매핑하고 qualification NOT_RUN을 분리했다. 시험 실행 기록은 없다. |

차단 사유:
- 선행 AIK-009·AIK-011 장부상 미완료
- API 엔드포인트·헤더·응답 형식 미검증 가정
- AIK-022/082/086 단계별 근거 없음

실행하지 않은 검증(NOT_RUN):
- UrllibTransport 실제 vendor 왕복
- 실제 키 인증(AIK-022)
- runtime·도구 실행과 제품 통합(AIK-082/086/044)
- Windows 실행(DPAPI vault 포함)

## AIK-014

- 구현: src/aik_host_codex에 인증 시작·상태·재연결·해제·취소 경로, host handle, 분리된 HostCapability, 계정 binding revision 대조, VM runtime 위임을 구현했다.
- 검증: tests/test_host_codex.py의 합성 runner·합성 VM runtime fixture와 로컬 Python 합성 프로세스 시험 범위의 개발 검증이다. 문서에는 실행 결과 기록이 없다. runner 출력 상한·프로세스 트리 종료·재관측은 독립 검토 2회 반영 후 Linux 단위 시험 통과, Windows Job Object 분기는 windows-2022 CI 외 NOT_RUN.
- 커밋: `861db1f`, `592a418`, `74b22f2`, `67fe942`
- 시험: `tests/test_host_codex.py` (38개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | 5개 GUI command의 코드 경로가 stub 없이 구현됐고 성공/실패는 합성 fixture로만 다룬다. fixture 결과를 실제 로그인 성공으로 기록하지 않는다. |
| 2 | 단위 범위 충족 | host-session handle만 사용하고 토큰·세션 파일을 읽지 않는다. OPENAI_API_KEY는 제외한다. absent/unsupported/expired를 별도 필드·코드로 보고한다. |
| 3 | 부분 | stale·재연결과 qualification NOT_RUN 분리는 구현됐다. 다만 fingerprint는 가명화 값이고, binding 상태는 메모리 전용이라 재시작 후 펜싱이 불완전하다. |

차단 사유:
- 선행 AIK-008·AIK-011의 완료 근거가 이 문서에 없음
- CLI 명령·상태 문구·계정 식별자 출력 가정을 공식 근거로 확인하지 못함(식별자가 없으면 binding 생성 불가)
- 영속 binding·reconciliation은 AIK-031/033/035/038 범위
- ProcessTreeUnconfirmed의 bridge native_code 매핑 미확인
- 실제 인증·사전 probe·governed 근거(AIK-022/082/086) 부재

실행하지 않은 검증(NOT_RUN):
- 실제 Codex CLI 왕복(SubprocessRunner)
- 실제 구독 로그인·만료·계정 전환(AIK-022)
- 사전 probe(AIK-082)·governed qualification(AIK-086)·제품 실행(AIK-044)
- Windows 실행(codex.cmd, CREATE_NO_WINDOW, taskkill /T /F, PATH/cwd/PATHEXT 해석)

## AIK-015

- 구현: src/aik_host_claude에 인증 시작·상태·실행 요청·해제·재연결 경로, 무작위 host handle, catalog/실측 분리 capability, 선택 계정 binding 검증을 구현했다.
- 검증: tests/test_host_claude.py의 합성 fake runner fixture와 로컬 Python 자식 프로세스 시험 범위의 개발 검증이다. 문서에는 실행 결과 기록이 없다. runner 출력 상한·프로세스 트리 종료·재관측은 독립 검토 2회 반영 후 Linux 단위 시험 통과, Windows Job Object 분기는 windows-2022 CI 외 NOT_RUN.
- 커밋: `78bf3b2`, `f956ccf`, `74b22f2`, `67fe942`
- 시험: `tests/test_host_claude.py` (51개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | 5개 코드 경로가 CommandSpec을 만들고 출력을 해석하며 stub이 아니다. 성공/실패는 합성 fixture로만 다루고 실제 실행 성공으로 기록하지 않는다. |
| 2 | 단위 범위 충족 | 토큰·설정 파일을 읽지 않고 handle과 fingerprint만 보관한다. host absent/unsupported/expired를 별도 host_state·reason으로 구분한다. |
| 3 | 부분 | configured/observed 모델 분리와 reconnect_required는 구현됐다. 그러나 선택 계정 생략 시에도 실행이 가능하고, 격리 single_active는 실측이 아닌 가정이다. |

차단 사유:
- 선행 AIK-008·AIK-011의 완료 근거가 이 문서에 없음
- claude auth status JSON·실행 출력·종료 코드 가정 미확인
- handle·revision·receipt가 메모리 전용이며 재시작 대조는 AIK-031/033/035/038 범위
- RunnerTerminationUnknown의 bridge 처리 전제 미확인
- 실제 근거(AIK-022/082/086) 부재

실행하지 않은 검증(NOT_RUN):
- 실제 Claude Code 구독 인증(AIK-022)
- 사전 probe·runtime 실행·제품 통합(AIK-082/086/044)
- SubprocessRunner의 Claude Code 대상 실행
- Windows 동작(.cmd, CREATE_NEW_PROCESS_GROUP 등, taskkill /T /F, launch 기한 종료)

## AIK-016

- 구현: aik_auth_lifecycle 인증 상태 기계를 구현했다. begin_run/placement_candidates 배치 제외, 키 회전·회수 표시, 잠금 정책·신호원, 강제 로그아웃, 삭제 tombstone, 이벤트를 포함한다.
- 검증: 합성 binding·주입 시계·fake adapter·FakeVaultBackend로 만료, 회전, 스레드 경합, 잠금 정책, 강제 로그아웃의 비완료 처리를 Linux 단위 시험으로 검사했다.
- 커밋: `5d4fc7f`, `8e4a5bb`
- 시험: `tests/test_auth_lifecycle.py` (27개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | 이 API를 경유하면 비valid 자원을 제외하고 재인증 후 재검증을 요구한다. 그러나 레지스트리를 직접 조회하면 우회된다. |
| 2 | 부분 | 새 실행은 새 credential_version을 받는다. 이전 실행 회수는 외부 record_observation에 의존하며 관측 루프와 영속화가 없다. |
| 3 | 부분 | 잠금 정책과 강제 로그아웃의 비완료·비환불 처리를 구현했다. Windows 세션 신호는 NOT_RUN이고 폴링 사이 잠금을 놓친다. |

차단 사유:
- 선행 AIK-009·AIK-010·AIK-011 장부상 미완료
- 연동하는 AIK-019(aik_pool) 장부상 미완료
- 상태·회수 목록 영속화와 레지스트리 비활성화 hook 미구현

실행하지 않은 검증(NOT_RUN):
- 실제 Windows 세션 잠금 신호
- 실제 공식 호스트·provider 만료/잠금 관측
- 실제 DPAPI vault와의 회전 연동

## AIK-018

- 구현: src/aik_broker에 grant 기반 scoped secret 해석(inject/run_with_credential)을 구현했다. adapter identity·workspace·task·lease·connection 재확인과 stdin/env 자식 주입·정리를 포함한다.
- 검증: tests/test_broker.py는 fake vault·fake 연결 원천·합성 lease로 거절 경로, 평문 비노출, 자식 주입·POSIX 정리를 검사하도록 작성됐다. 문서에 실행 결과 기록은 없다.
- 커밋: `d25aa52`, `e6617d5`
- 시험: `tests/test_broker.py` (34개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | worker에는 connection_id와 grant_id만 전달하도록 설계했다. 그러나 grant_id는 별도 bearer capability이고, 실제 총괄·worker 경로와 연결되지 않았다. |
| 2 | 부분 | broker 경로에서 adapter·workspace·task·lease·connection 일치를 검사한다. 동일 사용자 native 경로는 vault ADR trust profile로 링크했으나 그 문서는 확인하지 않았다. |
| 3 | 부분 | argv·env 비밀 차단, 파일 비기록, POSIX process group 정리를 구현했다. Windows는 직계 자식만 종료하고 Job Object는 미구현이며 NOT_RUN이다. |

차단 사유:
- 선행 AIK-004·009·010·017 미완료 부분
- 실제 ConnectionStore·DPAPI vault 연동 없음(fake로 대체)
- Windows 손자 프로세스 정리(Job Object) 미구현
- broker의 grant task와 lease 결속 대조를 발급자 책임에 의존

실행하지 않은 검증(NOT_RUN):
- Windows 자식 프로세스 정리·취소·환경 허용 목록·환경 블록 노출
- 실제 ConnectionStore·DPAPI vault 연동
- 실제 provider adapter·Electron main 연결

## AIK-019

- 구현: src/aik_pool에 (Pool, model) 경로별 메모리 inventory를 구현했다. 구현·계정·runtime·governance·시각·matrix 필드, TTL stale, 용량 unknown, 4단계 qualification을 포함한다.
- 검증: tests/test_pool.py는 공통 fake adapter·fake clock으로 다중 계정, 상태 전이, stale, 용량 unknown, qualification 무효화를 검사하도록 작성됐다. 합성 verifier는 로직 검증용이다.
- 커밋: `0389216`, `e8357f6`
- 시험: `tests/test_pool.py` (37개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | pool_id·connection_id를 분리하고, 같은 모델의 구독/API 경로를 model·pool_id·connection_id로 각각 조회한다. |
| 2 | 단위 범위 충족 | 별도 필드를 노출한다. 계정 변경 probe 시 qualification을 폐기하고 경로별로 독립 계산하며, 구현만으로 executable_now를 만들지 않는다. 다만 감지는 probe 시점에만 이뤄진다. |
| 3 | 단위 범위 충족 | TTL 초과·시계 역행은 stale로 처리한다. 용량 probe 부재·이상값은 unknown/None으로 두고 0이나 무제한으로 바꾸지 않는다. |
| 4 | 부분 | 단계 구분과 verifier 없는 qualified 거절을 구현했다. 086용 test-only candidate binding, AGS A2/VM gate 적용, 실제 qualification PASS는 없다. |

차단 사유:
- 실제 live qualification 근거 부재(AIK-086)
- AIK-022 인증·AIK-082 사전 probe 근거와의 대조 미구현
- inventory 비영속, 실제 provider probe 연결 없음
- 선행 AIK-011·016이 장부상 미완료

실행하지 않은 검증(NOT_RUN):
- 실제 provider·공식 호스트 로그인 probe
- 실제 live qualification
- Windows 11 동작

## AIK-020

- 구현: src/aik_capability에 connection별 목록 로드·검증된 수동 등록, provenance 필수 snapshot을 구현했다. (model, effort, tools, context) validator와 revision·stale 판정도 포함한다.
- 검증: tests/test_capability.py는 합성 모델명으로 거절 규칙, resource별 분리, provenance 비승격, 미지원 조합, TTL·binding 변경 전이를 검사하도록 작성됐다. 실행 결과 기록은 없다.
- 커밋: `857c60a`, `1c1dcd6`
- 시험: `tests/test_capability.py` (26개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | 제품 코드에 모델명이 없다. load_list와 catalog_ref가 필요한 register_manual로 resource별로 등록한다. |
| 2 | 단위 범위 충족 | snapshot 단위로 source_ref·observed_at·loaded_at을 기록한다. 출처와 시각의 실제 정확성은 형식만 검사한다. |
| 3 | 단위 범위 충족 | 4개 provenance를 필수 구분한다. requested·local_derived는 지원 근거로 쓰지 않고, 같은 이름도 connection별로 분리 판정한다. |

차단 사유:
- 실제 provider 모델 목록 조회 adapter 부재(공통 계약에 목록·도구 probe 없음)
- catalog_ref가 가리키는 검토 기록의 실재 미검증
- 선행 AIK-019 장부상 미완료

실행하지 않은 검증(NOT_RUN):
- 실제 provider·실계정 모델 목록 조회
- Windows 11 동작

## AIK-021

- 구현: src/aik_scope에 workspace별 명시 allowlist·공유 규칙 ScopeStore와 guard 경유 프로필 저장을 구현했다. 비밀 제외 설정 export/import와 재연결 대기 복원도 포함한다.
- 검증: tests/test_scope.py는 임시 폴더·합성 ID·FakeVault로 범위 판정, export 비밀 비포함, 빈 vault의 다른 사용자 모사 복원, 충돌·부분 실패를 검사하도록 작성됐다.
- 커밋: `88ca5fc`, `805633c`
- 시험: `tests/test_scope.py` (19개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | guard는 미허용 참조를 차단한다. 그러나 SettingsStore/ProfileStore 직접 저장은 막지 못해 violations로만 드러나고, broker·orchestrator와 연결되지 않았다. |
| 2 | 단위 범위 충족 | 정확한 키 allowlist로 검사하고, API 키·호스트 토큰·검증 시각·결속을 제외한다. 예상 밖 필드가 있으면 export를 거절한다. |
| 3 | 단위 범위 충족 | 해석 불가 vault 참조는 등록하지 않고 pending_restores 재연결 필요 상태로 복원한다. 실제 DPAPI 타 사용자 확인은 NOT_RUN이다. |
| 4 | 부분 | execution_ready·goals_and_evidence_restored는 항상 False이고 workspace는 미결속으로 복원한다. 같은 vault 경로는 재연결을 강제하지 않으며, 재결속은 AIK-084에 의존한다. |

차단 사유:
- 선행 AIK-010·018 미완료
- 직접 저장 경로 미차단, broker·orchestrator guard 미연결
- 재결속 AIK-084·전체 복원 AIK-058·GUI AIK-088 미구현
- 다중 파일 import 원자성 없음

실행하지 않은 검증(NOT_RUN):
- 실제 다른 Windows 사용자/기기의 DPAPI 참조 비해석 복원
- Windows 기본 경로·잠금
- 실제 aik_vault·공식 호스트 어댑터 연동

## AIK-023

- 구현: src/aik_profiles에 DirectorProfile·RoleProfile schema, 역할별 독립 revision 저장소, 승인 fallback만 허용하는 prepare_execution을 구현했다. provenance 기록과 validate CLI도 포함한다.
- 검증: tests/test_profiles.py는 임시 폴더·합성 모델·주입 시계로 저장, 역할 분리, 옵션 불일치, fallback 승인, 기록 조회를 검사하도록 작성됐다. inventory 등록은 harness로 넣는다.
- 커밋: `7178e4c`, `afaebcd`
- 시험: `tests/test_profiles.py` (29개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | connection·model·effort·tools·max_calls·budget과 순서 있는 fallback(최대 8)을 저장하고 settings 참조를 확인한다. |
| 2 | 단위 범위 충족 | 프로필별 행·revision을 두고 role·workspace 변경을 거절해, 총괄 변경이 worker·검토자 행을 덮어쓰지 않는다. |
| 3 | 부분 | prepare_execution이 capability를 검증하고 요청과 binding·provenance를 execution_records에 남긴다. 실행 경로 통합과 실제 ConnectionBinding 경로는 미검증이다. |

차단 사유:
- 선행 AIK-007·020 미완료
- 실행 경로에서 prepare_execution/require를 호출하는 통합 코드 없음
- FallbackApproval.approval_ref 실재·승인자 미검증
- settings와 분리돼 connection 삭제를 DB 수준에서 막지 못함

실행하지 않은 검증(NOT_RUN):
- Windows 기본 경로·잠금·CLI 정규 파일 판정
- 실제 provider·실계정 capability 조회
- 실제 aik_provider.ConnectionBinding 경로

## AIK-024

- 구현: src/aik_director에 supervisor 단일 owner·프로필 검증·probe·broker grant를 거치는 총괄 check/start/stop을 구현했다. SetupIssue 반환, 관측 비교, 이벤트 기록, health payload도 포함한다.
- 검증: tests/test_director.py는 실제 supervisor·inbox와 fake profile gate·runner·broker로 설정 문제, 중복 시작(별도 프로세스 포함), 관측 불일치, 정지 실패, 유휴 wake 0회를 검사하도록 작성됐다.
- 커밋: `51365a6`, `314e107`
- 시험: `tests/test_director.py` (33개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | worker 프로필·pool·placement 없이 check()와 start()로 총괄 연결을 검증·시작한다. runner는 fake다. |
| 2 | 단위 범위 충족 | 미설정·로그인·연결·capability·fallback 등을 field·code·action을 담은 SetupIssue로 반환한다. |
| 3 | 부분 | 중복 시작을 차단하고 시작·정지·실패·관측 이벤트를 기록한다. 기록은 event_sink 없으면 비영속이고, 관측값은 runner 보고에 의존한다. |

차단 사유:
- 선행 AIK-006·018·023 부분 구현
- 실제 provider runner·모델 호출 미구현
- health endpoint 서버·GUI 로그인/모델 선택 연결 없음
- grant 갱신 미구현, director 종료 후 runner 이중 실행 방지 없음

실행하지 않은 검증(NOT_RUN):
- Windows 11 msvcrt 잠금 중복 시작 차단·%LOCALAPPDATA% 경로
- 실제 ProfileStore·CapabilityInventory·CredentialBroker 결합
- 실제 모델·effort 관측

## AIK-025

- 구현: src/aik_inbox에 별도 SQLite inbox를 구현했다. message_id·goal_id·source cursor 기록, 중복·순서 역전 처리, lease·ack·fail, outbox, 재시작 회수, 상태 변화 시에만 깨우는 WakePolicy를 포함한다.
- 검증: tests/test_inbox.py는 임시 합성 DB로 중복, 순서 역전, 실제 자식 강제 종료 후 회수, hot journal rollback, 빈 큐 1000회 poll wake 0회를 검사하도록 작성됐다. 실행 결과 기록은 없다.
- 커밋: `353581a`, `9caa99f`
- 시험: `tests/test_inbox.py` (24개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | message_id·goal_id·source·event_cursor를 저장한다. 같은 message_id와 같은 cursor·내용의 재전송은 duplicate로 처리한다. |
| 2 | 부분 | 종류별 WakeSignal을 만들고 같은 상태로는 재호출하지 않아 유휴 무한 호출을 막는다. 실제 총괄 model 호출 루프와는 결합하지 않았다. |
| 3 | 단위 범위 충족 | 열 때 recover로 만료 lease를 회수한다. AckResult.goal_completion은 항상 not_determined이고, ACK와 작업 완료를 분리한다. |

차단 사유:
- 선행 AIK-005·024 미완료
- AGS/VM 이벤트 수신기·GUI 지시 입력·outbox transport 미연결
- 총괄 model 호출 루프와 wake callback 결합 없음

실행하지 않은 검증(NOT_RUN):
- Windows 11 msvcrt 잠금·%LOCALAPPDATA%·강제 종료 후 회수·hot journal 복구
- 실제 AGS/VM 이벤트 연결과 상류 재시도 계약 확인
- 대규모 큐 처리량·전원 차단

## AIK-026

- 구현: src/aik_controller에 VM 공개 명령으로 Goal을 위임·회수·보고하는 bounded loop, 순수 decide 결정 표, RunPolicy, GatewayCommandAuthority adapter를 구현했다.
- 검증: tests/test_controller.py에서 fake EngineApplication 위의 실제 VmBridge와 fake 위임·authority·wake·시계로 합성 상태 전이를 검사했다. 문서에는 실행 결과 기록이 없다.
- 커밋: `7ec1c93`, `6de6faa`
- 시험: `tests/test_controller.py` (42개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | Goal/Plan/Task는 VM prepare/revise/replan으로만 변경하고 DAG·ready 판정은 VM이 소유한다. status payload 계약은 가정이다. |
| 2 | 단위 범위 충족 | authority 검사 뒤 VM ready 작업만 run_once로 요청한다. blocked/unknown/validation_failed에 따라 정지·관측·replan을 결정하며 fake로 검증했다. |
| 3 | 단위 범위 충족 | model_reported는 기록만 한다. goal_complete는 passed GoalVerdict와 비어 있지 않은 evidence가 있을 때만 성립하도록 강제한다. |
| 4 | 단위 범위 충족 | A/B→C 입력 전달, 실패 후 replan·새 attempt, Goal Test, final_report 인계를 fake VM fixture로 검사했다. 실제 E2E는 AIK-044/059 gate다. |

차단 사유:
- 선행 AIK-025는 부분 구현이고 AIK-029·AIK-031의 완료 근거가 문서에 없음
- 실제 VM status는 aik_vm_bridge.engine_port 정규화로 고정 revision(32bb0f9) 소스와 대조했으나 final_report·전체 Goal 흐름은 실제 VM에서 미검증
- 실제 AuthorityGateway ExecutionDecision 반환 계약 미확인
- 미해결 effect 기억이 메모리 전용이라 재시작 내구성 없음

실행하지 않은 검증(NOT_RUN):
- 실제 모델·VM 설치물·AGS·provider E2E(AIK-044/059 합성 목표 gate)
- 실제 AuthorityGateway 세션 결합
- Windows 실행

## AIK-027

- 구현: src/aik_continuity에 append-only SQLite 지시·정정·설정 revision·VM 참조 checkpoint, handoff, VM status만 호출하는 Resumer를 구현했다. AGS 원장 참조는 미구현이다.
- 검증: tests/test_continuity.py에서 OS 임시 폴더 합성 DB와 fake VM read port, subprocess 재기동 모사로 검사했다. 문서에는 실행 결과 기록이 없다.
- 커밋: `f56057c`, `0cbd5e1`
- 시험: `tests/test_continuity.py` (22개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | 원문을 seq 순서로 보존한다. replace/supplement/withdraw 범위와 대상 규칙을 저장 시 검사하며, 재열기 후 보존을 합성 DB로 검사했다. |
| 2 | 부분 | 미완료 task·evidence는 VM status 재조회로 회수한다. 그러나 AGS 원장 참조는 미구현이고, 허용 범위는 인가 revision 비교만 한다. |
| 3 | 단위 범위 충족 | Resumer는 효과 명령 경로가 없다. 기존 Goal/attempt ID와 checkpoint revision으로 continue하고, 불일치 시 사용자 확인으로 정지한다. |

차단 사유:
- AGS 원장 참조·회수 경로 미구현
- 선행 AIK-025·AIK-026이 부분 구현 상태
- VM status 대조는 engine_port 경유로만 확인, continuity 재개 경로의 실제 VM 실행 미검증
- Controller·Director와의 checkpoint 저장 자동 연결 미구현

실행하지 않은 검증(NOT_RUN):
- 실제 VM 설치물 status payload 대조
- Windows 11 실행(msvcrt 잠금, %LOCALAPPDATA% 경로)
- 실제 총괄 모델의 문맥 축약
- 강제 종료 후 hot journal 복구

## AIK-028

- 구현: src/aik_switch/에 총괄 모델 교체 상태기계(active/switch_pending/reauth_required/decision_required), 사용자 정책 범위의 허용 fallback 판정, append-only 전환 journal(SQLite, application ID AIKS), aik_continuity handoff 연결을 구현했다. worker·reviewer 프로필은 바꾸지 않는다.
- 검증: tests/test_switch.py(32개) 합성·fake 단위 시험이 세션 Linux 전체 회귀에서 PASS. 독립 API 검토 finding을 수정했다(`c22e265`).
- 커밋: `15f2b5d`, `c22e265`
- 시험: `tests/test_switch.py` (32개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | 진행 중 호출이 있으면 switch_pending으로 두고 마지막 호출의 terminal(취소는 취소 완료) 관측에서만 적용한다. 같은 event 재전송은 duplicate로 효과가 없다. 실제 runner 세션 교체는 NOT_RUN. |
| 2 | 부분 | 사용자 정책의 fallback 순서·품질·도구·데이터·예산 범위로 판정한다. 품질 등급·비용·데이터 label은 사용자 지정값이며 실제 측정·예산 차감·데이터 집행은 하지 않는다. |
| 3 | 부분 | 이유·이전/새 binding·요청값·관측값(없으면 NOT_OBSERVED)·checkpoint·handoff 요약을 기록하고 worker 프로필은 유지한다. 실제 effort 관측은 runner 연결이 없어 NOT_RUN. |

차단 사유:
- 실제 runner·provider 연결이 없어 실제 모델 교체·인증 장애 신호·관측 model/effort가 미검증
- 진행 중 호출·대기 요청은 프로세스 메모리에만 있음(재기동 시 대기 요청 유실 처리)
- 선행 작업의 장부상 미완료(AIK-020/023/027)

실행하지 않은 검증(NOT_RUN):
- 실제 모델 호출과 runner 세션 교체
- provider 인증 만료/잠금 신호 연결
- Windows 잠금·기본 경로, 강제 종료 시험

## AIK-029

- 구현: aik_authority에 정책·프로필·VM 인가·위임·broker 범위의 교집합으로 허용하는 scoped tool gateway와 authorization mapper, effect identity·digest·효과 종류 규칙, 가정된 VM 효과 port 연결을 구현했다.
- 검증: fake 시계·lease checker·VM effect port와 합성 ID로 거부 경로, 동일 인가 재사용, 범위 확대, stale revision, 효과 종류 규칙을 단위시험 대상으로 작성했다. 문서에는 실행 결과 기록이 없다.
- 커밋: `92a73be`, `61f6ef5`
- 시험: `tests/test_authority.py` (31개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | delegate가 총괄 범위를 새 승인 없이 전달하고, 동일 인가를 재사용하는 경로를 fake 단위시험 대상으로 문서화했다. |
| 2 | 부분 | 도구·대상·효과·입력 revision 이탈은 SCOPE_ESCALATION·STALE_INPUT_REVISION으로 거절한다. VM 공개 계약 코드 대응은 가정이며 설치형 VM과 대조하지 않았다. |
| 3 | 부분 | handle만 전달하고 worker 위임 금지와 예약 도구 거절을 구현했다. 이름 필터는 best-effort이며, 같은 프로세스 안 handle 보유자와 OS 격리는 처리하지 않는다. |
| 4 | 부분 | digest를 Attempt·인가 revision·lease에 결속하고 효과 종류 3종을 구분한다. VM 효과 권위 port는 upstream에 없는 가정된 extension이다. |

차단 사유:
- VM 효과 권위 extension(record_intent/receipt/read_effect)이 upstream 공개 표면에 없음
- VM authorize 결과의 scope 형식 파싱 미구현
- 선행 AIK-004(blocked)·AIK-031(todo) 미완료
- 요청 시점 grant 재검사 미구현, 상태가 프로세스 메모리에만 있음
- AIK-004 IPC·Electron main·제품 GUI 미연결

실행하지 않은 검증(NOT_RUN):
- 설치형 VM 인가·효과 권위 연동
- VM 공개 계약 코드 대응 대조
- Windows 실행
- 실제 provider·broker 연동
- 재시작 내구성 검증

## AIK-030

- 구현: src/aik_jev에 JEV(TypeSafe 공개 계약 0.2.0) bridge를 구현했다. RawPrompt는 원문을 바꾸지 않고 sha256·길이로 결속하며, CommonReq는 코드가 확인한 metadata(작업 모드·대상·스킬 후보·제약)만 canonical JSON으로 담고 RAW 본문을 넣지 않는다. JevClient는 GET /v1/models·POST /v1/systemone을 재시도·redirect·모델 fallback 없이 호출하고 응답을 엄격 검증한다. JevBridge는 ON이면 RAW 원문과 REQ로 choice 질문을 보내 추천·확률·usage·관측 모델을 provider_observed로 기록하고, OFF·호출 불가·응답 거절이면 JEV 없이 같은 vendor의 품질 통과 최저비용 분류 후보만 기록한다(직접 호출하지 않음). AGENT의 최종 선택은 별도 append-only 기록(model_reported)으로 남기며 추천과 다르거나 후보 밖이어도 거절·덮어쓰기하지 않는다.
- 검증: tests/test_jev.py 18개가 합성 데이터·fake transport로 RAW 보존, RAW_REQ_MISMATCH, ON 성공·관측 모델 분리, OFF 호출 0회와 fallback 후보 규칙, HTTP 오류·timeout별 상태와 재시도 0회, 응답 계약 위반, 토큰 비노출, AGENT 선택 분리, request_id 재호출·충돌, journal append-only를 검사한다. 추가로 tools/probe_jev.py로 실제 서비스에 합성 요청 1건을 보내 classified(요청 jev-latest, 관측 jev-1.13.0)와 AGENT 선택 별도 기록을 관측했다(evidence/jev/2026-10-13-live-probe).
- 커밋: `e228314`
- 시험: `tests/test_jev.py` (18개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | 실제 JEV 공개 계약(0.2.0)·모델 목록·systemone 응답을 관측했고 RAW 보존·확인된 metadata만의 REQ를 구현했다. VM/AGS 두 고정 계약 저장소에 JEV 경계가 없어 내부 편입은 미완료이며 upstream 변경 참조는 없다. |
| 2 | 단위 범위 충족 | JEV 분류(provider_observed)와 AGENT 최종 선택(model_reported)을 별도 테이블·필드로 기록하고 실호출 진단에서도 둘이 분리됨을 관측했다. |
| 3 | 부분 | OFF·호출 불가 시 같은 vendor의 품질 통과 최저비용 후보를 기록하고 선택을 강제하지 않는다. fallback 분류 LLM의 실제 실행과 벤더 품질 기준 판정은 NOT_RUN이다. |

차단 사유:
- VM/AGS 고정 계약에 JEV 연동 경계 없음(편입 불가, upstream 요청 안 함)
- fallback 분류 후보의 벤더 품질 기준 판정·실행 경로 미연결
- 선행 AIK-031·AIK-032의 실제 governed 실행 미검증

실행하지 않은 검증(NOT_RUN):
- token_resolver Bearer 직접 부착과 vault 연결
- 실제 사용자 요청·스킬 manifest에 대한 분류 품질
- fallback LLM 실제 실행
- Windows 실행

## AIK-031

- 구현: aik_vm_bridge에 typed request/receipt, surface preflight, 버전 handshake, 오류 매핑, TaskExecutionBinding, 실제 EngineApplication 어댑터를 구현했다. durable binding이 없으면 run_once를 SEAM_UNAVAILABLE로 차단한다.
- 검증: fake VM port 단위시험을 작성했다. 32bb0f9 소스를 import할 수 있을 때만 임시 원장과 합성 프로젝트로 공개 façade 적합성 시험을 수행하며, 불가하면 skip(NOT_RUN)된다. 실제 upstream 적합성 시험이 고정 revision 소스(PYTHONPATH)에서 PASS; 설치형 wheel·Windows 실행은 CI secret(VM_UPSTREAM_TOKEN) 부재로 NOT_RUN.
- 커밋: `7136475`, `341e9f9`, `db4e7ec`, `81e98c0`
- 시험: `tests/test_vm_bridge.py` (55개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | 변경은 port의 공개 command로만 하고, bridge 소스에 SQL·DB 연결이 없음을 테스트로 확인한다고 문서화했다. |
| 2 | 부분 | VmRefs에 ID·revision·digest·history_cursor를 보존한다. request ID는 VM에 전달하지 않으며, authorize는 source와 plan revision만 전달한다. |
| 3 | 부분 | vm_code로 원래 코드를 보존하고 불명확한 경우는 unknown으로 둔다. STALE_EXECUTION_INPUT 등의 known_failure 분류는 실제 오류 목록과 대조하지 않은 가정이다. |
| 4 | 미충족 | 실제 EngineApplication에 bind_execution/read_execution_binding이 없어 bound run_once·관측·취소·inventory가 fail-closed로 차단된다. |

차단 사유:
- upstream durable binding extension 부재(AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED)
- 실제 AttemptDetail에 goal_revision_id가 없어 owner query 정보 부족
- AGS manifest 부재(AGS_HOST_INTEGRATION_MANIFEST_MISSING)
- status는 engine_port 정규화로 구현·대조, final_report·run_once 실제 Goal 경로 미검증
- 선행 AIK-004 blocked 상태

실행하지 않은 검증(NOT_RUN):
- 설치형 VM wheel 호출
- Windows 실행
- provider 연동
- durable binding extension 검증
- surface/schema revision 선언값의 설치물 확인

### AIK-031 추가 기록 — VM 설계 seam 기준 hosted 경로 (사용자 지시: "VM 설계대로 구현되었다 가정")

- **구현:** VM 설계에는 `bind_execution` 확장이 없다. 그래서 VM 설계 seam만 써서 효과 전 binding을 AIK 쪽에 durable하게 남기는 hosted 경로를 추가했다.
  - VM seam: `EngineApplication(runtime=...)` 주입, `current_operation_trace_scope()`, owner query(`attempt_detail`·`status` join).
  - 구성:
    - `AikHostedRuntime`: VM `CodexRuntimePort` 구현
    - `ExecutionBindingStore`: `aik_runtime.binding_journal`
    - `VmBridge.run_once_hosted`/`confirm_attempt`
    - `build_hosted_application`
  - 동작:
    - 원래 계정으로만 재개·중단한다.
    - thread ID 충돌은 거절한다(fail-closed).
    - write-ahead in-flight 행으로 실행 중 프로세스가 중단돼도 차단을 유지한다.
    - 결과 불명이면 durable하게 기록하고 reconcile 전까지 막는다.
  - 기존 확장 기반 경로는 바꾸지 않았다.
- **검증:**
  - `tests/test_vm_hosted_runtime.py`(39개)와 `tests/test_vm_hosted_runtime_review.py`(24개): fake VM 설계 계약 기준, 세션 Linux 전체 회귀 PASS.
  - 실제 upstream 적합성 (f)(g)(h): VM 32bb0f9 소스(PYTHONPATH)에서 PASS.
    - (f) `CodexRuntimePort` 9개 메서드 시그니처 일치
    - (g) 실제 trace scope 읽기
    - (h) 실제 `EngineApplication`이 hosted runtime을 받아들임. 생성만 하며 효과는 없다.
  - 독립 API 검토 R1 finding 10건을 모두 수정했다(`1e3bbd6`).
- **커밋:** `0b7960a`, `1e3bbd6`
- **완료 기준 4 판정 변경:** 미충족 → **부분**. hosted 경로로 효과 전 durable binding과 원래 binding 기반 관측·취소·inventory를 구현했다. 그러나 실제 설치형 VM에서 Task를 실행하는 검증은 NOT_RUN이다.
- **남은 차단:**
  - 실제 RuntimeJob 실행 검증 NOT_RUN. VM 1.0 RuntimeJob은 Windows 전용이다.
  - supervisor 스레드에서 trace scope가 존재한다는 점은 VM 설계 가정이며, 실측하지 않았다.
  - AGS A2 전환 전이라 governed dispatch에 도달하지 못한다(AIK-081).
  - `AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED`는 설치형 검증 전까지 유지한다.
  - 통합 완료가 아니다.

### AIK-031 추가 기록 — Windows hosted E2E 진단 (CI run #23, `15940c5`)

- **도구:** `tools/diagnose_hosted_e2e.py`, CI job `hosted-e2e-diagnostic`(windows-2022)
- **실행 내용:** VM 32bb0f9 소스로 합성 Goal·Plan을 준비한 뒤, VM Windows 전용 RuntimeJob supervisor로 Task를 dispatch했다.
  - AIK hosted runtime이 VM `FakeCodexRuntime`을 감쌌다.
  - 모델·계정·네트워크 호출은 없었다.
- **결과: `DIAGNOSTIC_PASS`.** 아래를 확인했다.
  - VM worker 스레드(`flowmarshal-worker_turn-*`, 메인 스레드 아님)의 `create_thread`·`start_turn`이 hosted runtime을 지났다. 이때 trace scope의 Attempt·intent·call ID를 읽었다.
  - 각 효과 호출 순간에 해당 Attempt의 durable in-flight 행이 이미 있었다(효과 전 기록).
  - 성공 뒤 in-flight 행이 정리됐다.
  - `confirm_attempt`가 VM owner query와 `status` join으로 binding을 확정했다.
  - 이 확인으로 "VM supervisor 스레드에서도 trace scope가 존재한다"는 VM 설계 가정이 Windows 실제 실행에서 관측됐다.
- **새로 관측한 설계 제약:** VM은 역할 실행 중 호스트의 재진입(`attempt_detail`)을 `CORE_CAPABILITY_DENIED`로 거절한다. 그래서 효과 시점의 placement Task는 `HOSTED_TASK_PENDING`으로 두고, owner 확인 때 확정한다.
- **한계:**
  - VM wheel이 개발자 모듈(`e2e_qualification` 등)을 빼므로, VM은 **소스에서 import**했다. 설치형 wheel 근거가 아니다.
  - governance gate는 진단용 허용 gate다. AGS가 아니다.
  - 이번 run은 Task 완료까지 진행했지만 독립 Goal Test 판정(`goal_verdict_id`)에는 시간 안에 도달하지 못했다(null). 후속 run #26에서 도달했다(아래).
  - 실모델, Windows 11, AGS A2는 NOT_RUN이다.
  - 통합 완료가 아니다.

### AIK-031 추가 기록 — Windows 진단의 Goal 판정 도달 (CI run #26, `dc77b6d`)

- **결과:** 같은 진단 경로(VM 32bb0f9 소스, VM `FakeCodexRuntime`, 진단용 허용 gate)에서 Task 완료 뒤 독립 Goal Test가 실행됐다. evidence-backed GoalVerdict `satisfied`가 기록됐다(`goal_verdict_id` 값 있음, 마지막 action `completed`). 앞의 여섯 검사도 모두 true였다.
- **run #25에서 막힌 원인:** Goal Test binding 고정(`materialized`)이 581번 반복되고 실행되지 않았다.
  - VM은 Goal Test step의 cwd를 `resolve()`로 정규화해 binding에 저장한다.
  - Windows runner의 임시 경로는 8.3 짧은 이름(`RUNNER~1`)이다. 그래서 매 tick 공급한 step이 저장된 binding과 달랐고, VM이 binding을 다시 고정했다.
  - run #26에서 `work_path_resolved_differs: true`로 이 차이를 관측했다.
- **조치:** AIK 진단만 고쳤다. 정규화된 작업 경로에서 시작하고, binding이 생긴 뒤에는 step을 다시 공급하지 않는다. VM은 고정 계약으로 보고 수정하지 않았다.
  - 관측: VM 자체 E2E 루프처럼 매 tick 같은 step을 공급하는 호출자는 Windows 짧은 경로에서 같은 반복을 겪을 수 있다.
- **한계:** VM 소스 import(설치형 wheel 아님), AGS 아님, 실모델·Windows 11 NOT_RUN. 통합 완료가 아니다.

## AIK-032

- 구현: src/aik_vm_bridge/governance.py에 VM이 이미 내린 governance gate 결과의 표시용 projection을 구현했다. run_once 결과의 blocker_code로만 상태·phase를 정하고(GATE_PENDING/BLOCKED/FAILED/CONTRACT_MISMATCH, 그 밖의 GOVERNANCE_*는 unrecognized로 fail-closed), detail은 비권위 진단 텍스트로 사유 코드·검사 ID·적합성 검사 ID만 파싱한다. final_report의 governance_plugin_tasks를 출처(local_derived·plugin_manifest_file 자기 보고 label)와 함께 순서대로 투영하고, status의 blocked Task ID를 사유 없이 돌려준다. AIK 판정·cache·재시도·gate 생략 fallback은 없다.
- 검증: tests/test_vm_governance.py 20개가 합성 BridgeReceipt로 네 코드 매핑, 미지 GOVERNANCE_* fail-closed, 비governance·EXTERNAL_EFFECT_UNKNOWN·non-blocked는 투영 안 함, 응답 형식 오류, detail 파싱과 detail이 상태를 바꾸지 않음, final_report 투영·키 누락 CONTRACT_MISMATCH, status blocked 목록을 검사한다.
- 커밋: `31609e1`
- 시험: `tests/test_vm_governance.py` (20개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 이 환경 검증 불가 | 두 phase gate의 집행은 VM이 한다. AIK는 결과를 투영만 하며 실제 AGS 설치·VM run_once 결합 실행이 없다. |
| 2 | 부분 | VM이 보고한 차단 사유(blocker_code·detail)를 그대로 노출하고 생략 fallback을 만들지 않는다. contract mismatch의 phase·operation.no_effect·적합성 검사 목록은 VM 공개 읽기 API가 없어 노출하지 못한다(UNREACHABLE_FACTS). |
| 3 | 단위 범위 충족 | VM 공개 경로(run_once·final_report·status)만 읽고 AIK 판정·cache·Task 완료 권위를 만들지 않는다. VM History는 SQL로 읽지 않는다. |

차단 사유:
- AIK-081 A2 호환 AGS plugin 미확보
- VM 공개 표면에 gate History 읽기 API가 없어 일부 사유(phase·no_effect·적합성 목록) 비공개
- 선행 AIK-031 hosted 경로의 실제 governed 실행 미검증

실행하지 않은 검증(NOT_RUN):
- 실제 설치형 AGS·A2 profile·적합성 실행
- 호환 plugin과 manifest/관측 불일치의 설치형 검증
- 실제 VM run_once/final_report 결합
- Windows 실행

## AIK-033

- 구현: src/aik_placement에 VM ready·인가 입력(ReadyTask) 기반 placement planner, 후보 프로필 선호 순서 eligibility filter(역할·모델·effort·도구 capability·route 자격·신선도·capacity·같은 프로젝트 직렬), 병렬 근거(ParallelQualification) 검사, append-only 결정 journal(inventory revision 포함)을 구현했다. unknown 배치는 capacity와 직렬 자리를 계속 점유한다.
- 검증: tests/test_placement.py 15개가 합성 Pool·ExecutionProfile·RuntimeStore와 fake registry/inventory로 조건별 배치, candidate/qualification 분리, 멱등, journal append-only, 동시 plan capacity, unknown 점유를 검사한다.
- 커밋: `8f4b942`
- 시험: `tests/test_placement.py` (15개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | ReadyTask는 VM이 준 ready·인가 ID만 담고 planner는 DAG·재시도·다음 Task 선택을 하지 않는다. 같은 Attempt 재요청은 멱등, 다른 Task면 ATTEMPT_ALREADY_PLACED로 거절한다. |
| 2 | 부분 | 역할·모델·effort·도구 capability와 pool 상태는 검사한다. AGS/AGENT 역할 계약의 host·전문역량·데이터 범위 요구는 입력 계약이 없어 검사하지 않는다. |
| 3 | 단위 범위 충족 | 같은 프로젝트에 활성·unknown 배치가 있으면 SAME_PROJECT_SERIAL로 대기한다. 병렬은 qualified route와 일치하는 inventory revision 근거가 있을 때만 허용하고 결정에 기록한다. 독립성 판단 자체는 VM ready 집합에 맡긴다. |
| 4 | 부분 | 운영 배치는 registry의 placement_eligible(qualified) route만, candidate route는 qualification 목적에서 명시 선택할 때만 쓴다. candidate 경로의 A2 gate·budget/lease 적용은 placement 밖(AIK-032 미구현, AIK-034 별도)이다. |

차단 사유:
- 선행 AIK-019·020·023·031·032·040 미완료
- AGS 역할 계약의 host·전문역량·데이터 범위 입력 부재
- 실제 qualified route 없음(AIK-086 미실행)
- A2 gate 미연결(AIK-032)

실행하지 않은 검증(NOT_RUN):
- 실제 ResourcePoolRegistry·CapabilityInventory와 실 provider 관측 통합
- Windows 실행
- 다중 프로세스 동시 배치

## AIK-034

- 구현: src/aik_lease에 runtime store 사용권 예약과 budget admission 연결(lease id = reservation id), fencing token 기반 낡은 owner 거절, provider 호출 전 dispatch 의도 기록(activate), 갱신·반환 3분기·만료(효과 전 expired / dispatch 뒤 unknown)·unknown 해소, crash 사이 고아 정리(reconcile)를 구현했다.
- 검증: tests/test_lease.py 16개가 임시 RuntimeStore·BudgetStore·주입 시계로 스레드 동시 예약 capacity, TTL 만료 분기, stale token, unknown 재발사 거절, budget wait/rejected/예외 보상, reconcile, release 분기, dispatch 의도 뒤 crash를 검사한다.
- 커밋: `8f4b942`
- 시험: `tests/test_lease.py` (16개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | runtime store BEGIN IMMEDIATE 안에서 pool capacity를 검사하고 budget admission이 예산·동시성을 검사한다. 두 DB는 한 commit이 아니어서 보상·reconcile로 맞추며, 다중 프로세스 경합은 이 태스크에서 시험하지 않았다. |
| 2 | 단위 범위 충족 | lease는 VM task/attempt를 가진 placement와 pool·worker를 참조하고, 모든 변경은 (lease_id, fencing_token)으로 검사해 낡은 token·더 새 lease가 있는 owner를 거절한다. |
| 3 | 단위 범위 충족 | 만료는 provider 종료가 아니다. dispatch 의도·job이 기록된 lease는 만료 시 unknown이 되고 예산 hold와 capacity를 유지하며, unknown이면 같은 placement의 새 lease를 거절한다. 종료 관측 뒤에만 released로 간다. |

차단 사유:
- 선행 AIK-005·033·040 미완료
- 실제 provider job 시작·관측 연결 없음(dispatch 의도 순서는 호출자 책임)
- 다중 프로세스·Windows 잠금 경합 미검증

실행하지 않은 검증(NOT_RUN):
- Windows 실행·msvcrt 잠금
- 다중 프로세스 경합
- 실제 provider job 수명

## AIK-035

- 구현: src/aik_dispatch에 dispatch 골격을 구현했다. DispatchIdentity(request_id·contract digest·VM project/task/Attempt·lease·fencing·pool·route·ConnectionBinding·canonical cwd·input digest·경로, 관측된 권한·same-user는 관측이 있을 때만)와 digest, append-only DispatchJournal, NativeDispatcher(lease dispatch intent와 reserved 기록을 provider 호출보다 먼저 남기고 GuardedAdapter.start_job → 같은 연결 receipt 확인 → job id를 lease·observer에 결속한 sent_ack → 같은 adapter 관측으로 started; 예외·다른 연결 응답은 send_unknown·재전송 없음, guard의 known_failure는 rejected로 lease 반환, 같은 request 재호출은 provider 재호출 없음, 재시작 시 reserved는 send_unknown), ApiToolLoop(모델 호출·도구 호출 상한, 예산·취소 확인, 도구마다 intent→DevTools(AIK-029 gateway)→receipt→결과 반환 기록, EffectUnknown은 정지, resume은 intent만 있는 호출을 재실행하지 않고 receipt 뒤 미반환 결과만 DevTools 재생으로 재전송)를 둔다.
- 검증: tests/test_dispatch.py 18개가 실제 RuntimeStore·BudgetStore·LeaseManager·WorkerObserver·DevTools(합성 workspace·fake authority)와 GuardedAdapter로 감싼 fake provider, fake model port로 상태 순서와 intent 선행, identity 불일치·중복·충돌, send_unknown·rejected·recover, 다른 연결 receipt 거절, API 루프의 상한·예산·취소·허용 밖 도구, intent만 남은 crash와 receipt 뒤 crash의 재생(파일 효과 1회), journal append-only를 검사한다.
- 커밋: `c393880`
- 시험: `tests/test_dispatch.py` (18개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | request_id·contract digest·Attempt·lease·fencing·연결·provider job id를 identity digest와 journal 이벤트로 결속하고 같은 request의 다른 identity는 CONFLICT로 거절한다. |
| 2 | 단위 범위 충족 | reserved(예약·dispatch intent)·sent_ack(전송 ACK)·started(관측된 실행 시작)를 별도 이벤트로 남긴다. |
| 3 | 부분 | API 경로는 도구 요청→DevTools(AIK-029 gateway)→결과 반환→다음 모델 호출을 상한·예산·취소와 함께 수행한다. native 경로의 기존 VM runtime 재사용·실제 권한/trusted same-user 관측과 VM/AGS 공개 command 연결은 없다(관측 칸은 None). |
| 4 | 부분 | identity에 route·connection·pool·lease·cwd·입력 digest를 고정하고 관측은 같은 GuardedAdapter(같은 연결)로만 한다. 재시작 뒤 같은 계정으로의 read/cancel은 관측기·복구 쪽 계약이며 실제 provider로 검증하지 않았다. |
| 5 | 부분 | tool_call별 intent·receipt·returned를 구분하고 같은 결과는 DevTools 재생으로 재전송하며 효과를 다시 실행하지 않는다. 결과 보존은 DevTools 인스턴스 메모리에 의존해 프로세스 재시작 뒤 재생은 미검증이고, 실제 API 모델 호출은 없다. |

차단 사유:
- 선행 AIK-029·031·032·084·085의 실제 결합과 AIK-018 미완료
- 실제 provider adapter·API 모델 호출 코드 없음(fake port)
- native 경로의 VM runtime·hosted runtime 결합과 권한/same-user 관측 없음

실행하지 않은 검증(NOT_RUN):
- 실제 provider job·API 모델 호출
- VM run_once·AGS gate 결합
- 프로세스 재시작 뒤 DevTools 재생
- Windows 실행

## AIK-036

- 구현: src/aik_observer에 lease 단위 provider job 관측기(별도 append-only SQLite), event cursor·중복·같은 sequence 충돌·gap 재조회, provider 관측/요청/model_reported 값 분리, usage normalizer(UNKNOWN과 confirmed 0 구분), terminal 관측 뒤 budget 정산을 구현했다.
- 검증: tests/test_observer.py 14개가 합성 이벤트·fake JobPort/EventSource·주입 시계로 재접속, 중복, 충돌, gap 복원/미복원, 부분 출력만으로 미완료, usage 미제공 vs 0, 관측/요청 분리, 정산 1회와 late를 검사한다.
- 커밋: `74adac1`
- 시험: `tests/test_observer.py` (14개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | 요청 model/effort는 client_requested로, provider_observed state 이벤트 값만 observed로(출처 sequence 포함), model_reported 값은 진단 칸에 따로 저장한다. job 상태는 provider receipt로만 기록한다. |
| 2 | 단위 범위 충족 | 저장된 연속 cursor부터 읽고 같은 digest 중복은 무시, 같은 sequence 다른 digest는 충돌로 멈춘다. gap은 기록 후 한 번 재조회로 메우고 못 메우면 gap_unresolved로 남긴다. |
| 3 | 단위 범위 충족 | usage 키가 없으면 UNKNOWN, 명시 0은 confirmed(0)이다. 완료는 provider JobState terminal 관측일 때만이며 output_delta·HTTP 성공만으로는 완료하지 않는다. |

차단 사유:
- 선행 AIK-035·082 미완료
- 실제 provider stream·receipt adapter(OpenAI·Anthropic·native host) 연결 없음

실행하지 않은 검증(NOT_RUN):
- 실제 provider 스트림·재접속
- Windows 실행
- 다중 프로세스 접근

## AIK-037

- 구현: src/aik_evidence에 검증 읽기 기반 산출물 수집, append-only identity·SHA-256·Attempt·lease·접근 범위 기록, write-once content-addressed 관리 보관소, callback dedup·충돌 기록, 보관소 재확인(intact/missing/tampered), 비밀 제거 파생본 별도 identity·provenance, 덮어쓰지 않는 내보내기와 digest 재확인을 구현했다.
- 검증: tests/test_evidence.py 10개가 한글·공백 경로의 합성 파일로 수집·재시작, dedup, 충돌 무덮어쓰기, 다른 workspace·탈출·링크 거절, 변조·삭제 감지, 파생본 분리, 비UTF-8 거절, 내보내기 재확인·덮어쓰기 거절, append-only·외부 DB 거절을 검사한다.
- 커밋: `74adac1`
- 시험: `tests/test_evidence.py` (10개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | artifact identity, sha256, size, 생성 vm_attempt_id·lease_id·callback_id, workspace·상대 경로·access_scope를 append-only로 저장한다. |
| 2 | 단위 범위 충족 | 같은 callback·같은 결과는 기존 기록을 돌려주고, digest나 메타데이터가 다르면 CALLBACK_CONFLICT를 기록하고 기존 기록을 덮지 않는다. |
| 3 | 부분 | 근거 접근·무결성 재확인과 권위 digest 보존, 비밀 제거 파생본의 별도 identity·digest·변환 provenance는 구현했다. 원본 보관이 정책상 불가할 때 처음부터 비밀 없는 근거를 생산하는 경로는 없다. |
| 4 | 부분 | 관리 보관소 인계, 새 파일 내보내기, 재시작 뒤 bytes/digest 확인은 구현했다. GUI 열기·저장 연결과 후속 Task 입력 binding(AIK-084·VM 공개 참조)은 연결하지 않았다. |

차단 사유:
- 선행 AIK-017·036·084 미완료
- GUI 열기·저장 연결 없음
- 보관 범위·기간 정책 미정
- 후속 Task 입력 binding 미연결

실행하지 않은 검증(NOT_RUN):
- GUI 열기·저장
- Windows 링크·권한 동작
- 실제 worker 반환 흐름

## AIK-038

- 구현: src/aik_lifecycle에 scope(global·pool·project) pause/drain/resume gate와 lease 예약 앞단 GatedLeaseManager(예약 경합 시 즉시 반환), owned/borrowed worker 등록부, cancel(취소 요청 기록→지원 시 interrupt, 미지원 시 job 유지, lease는 draining만)·settle_cancel(관측 terminal에서 cancel_confirmed/late_success/terminal_failed 구분 후 반환)·teardown(미해결 도구 효과·필수 근거·provider 종료 확인 뒤 owned만 종료, borrowed는 반환, 그 뒤 임시 데이터 정리), ExecutionBindingStore/AikEffectJournal 효과 adapter, append-only lifecycle 원장을 구현했다.
- 검증: tests/test_lifecycle.py 18개가 실제 RuntimeStore·BudgetStore·LeaseManager·WorkerObserver(합성)와 fake port로 pause 즉시 차단·resume, 예약 경합, project scope, fail-closed 조회, 취소 전 dispatch 없음, interrupt 응답은 종료 아님, 미지원 취소, interrupt 실패 unknown, 취소 직전 결과, 늦은 성공, owned/borrowed 처리, 미해결 효과·미보관 근거·공유 worker의 정리 보류를 검사한다.
- 커밋: `31609e1`
- 시험: `tests/test_lifecycle.py` (18개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | pause/drain 직후 GatedLeaseManager가 신규 예약을 막는다(경합 grant도 즉시 반환). 실행 중 job은 AdapterCapabilities.job_interrupt 공개 계약이 SUPPORTED일 때만 interrupt하고 아니면 그대로 둔다. |
| 2 | 부분 | 취소 요청·interrupt 응답·취소 확인·늦은 성공·실패 종료를 서로 다른 이벤트로 남긴다. usage는 observer 정산을 그대로 두며 시험의 정산 대상은 합성 sink다. evidence 최종 회수는 teardown의 보관 확인으로만 다룬다. |
| 3 | 단위 범위 충족 | owned worker만 worker_port.terminate를 부르고 borrowed는 terminate 없이 worker_returned만 기록한다. 같은 worker의 다른 lease가 남아 있으면 멈춘다. |
| 4 | 부분 | in-flight·unknown 효과와 receipt 대기 효과, 필수 근거 미보관 시 teardown_complete와 cleanup을 보류한다. 실제 worker 반환 흐름과 effect journal 결합은 fake adapter 시험뿐이다. |

차단 사유:
- 선행 AIK-035 dispatch 미완료(골격만)
- 실제 provider adapter interrupt/observe와 worker 종료·정리 구현 없음
- usage 정산의 실제 BudgetStore.settle 결합 미시험

실행하지 않은 검증(NOT_RUN):
- 실제 provider 취소·관측
- 실제 worker 종료·정리
- VM/AGS 결합
- Windows 실행

## AIK-039

- 구현: src/aik_recovery에 재시작 뒤 한 번 호출하는 RecoveryReconciler와 append-only RecoveryJournal을 구현했다. holding lease마다 저장된 request/receipt로 같은 계정에서 먼저 관측해 terminal이면 반환, 실행 중이면 유지, 관측 실패면 unknown 유지·재발사 없음, dispatch 기록 없는 reserved는 VM attempt가 terminal/absent일 때만 반환, job ID 없는 dispatch 의도는 unknown으로 VM unknown 절차에 넘긴다. 도구 효과는 receipt 없으면 mark_unknown(재실행 없음, 비멱등은 확인 전 중단), receipt 뒤 결과 미반환이면 같은 결과 재전송, 취소 뒤 늦은 receipt는 기록만 한다. live→unknown 전이는 LeaseManager.mark_unknown(fencing·revision 검사)으로 한다.
- 검증: tests/test_recovery.py 11개와 tests/test_lease.py의 mark_unknown 시험이 실제 RuntimeStore·BudgetStore·LeaseManager·WorkerObserver(합성)와 fake VM/job/effect port, 주입 시계로 dispatch 경계 crash, terminal 관측 반환, 실행 중 유지, 관측 실패 unknown 유지, receipt 전 crash, receipt 뒤 응답 전 crash, 취소 뒤 늦은 receipt, 재전송 실패 재시도, journal append-only, 두 번째 reconcile 멱등을 검사한다.
- 커밋: `080d185`
- 시험: `tests/test_recovery.py` (11개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | lease·placement·observer 기록을 VM attempt_state port 결과와 대조하고 provider 관측으로만 반환한다. VM port는 fake이며 실제 VM 공개 조회 adapter가 없다. |
| 2 | 단위 범위 충족 | 불명확한 lease·효과는 재실행하지 않고 unknown으로 두어 VM unknown 절차로 넘긴다. retry는 만들지 않는다. |
| 3 | 단위 범위 충족 | 모든 판단을 planned/applied/failed와 이유·관측 revision·원래 근거 ID로 append-only 기록하며 기존 기록을 덮지 않는다. |
| 4 | 부분 | tool_call의 receipt 유무와 결과 반환 여부로 재전송과 재실행을 구분하고 비멱등 불명은 중단한다. 실제 ExecutionBindingStore/AikEffectJournal adapter와 원래 route 재관측은 fake port 시험뿐이다. exactly-once를 주장하지 않는다. |

차단 사유:
- 선행 AIK-035 dispatch 미완료(골격만)
- VM attempt_state·effect port의 실제 adapter 없음
- 실제 provider 재관측 미실행

실행하지 않은 검증(NOT_RUN):
- 실제 provider 재관측
- 실제 VM 공개 조회·unknown 절차 결합
- 다중 프로세스 동시 reconcile
- Windows 실행

## AIK-040

- 구현: src/aik_budget에 locked 트랜잭션 admission guard를 구현했다. 호출 수·deadline·동시성·비용 hold·token stop·429·quota, basis 구분 append-only 정산, quota 상태 조회를 포함한다.
- 검증: tests/test_budget.py는 합성 profile·RouteStatus·주입 시계로 스레드·두 프로세스 경합, 429, quota 미확인, 지연 청구 대체, append-only를 검사하도록 작성됐다.
- 커밋: `47f3018`, `b08454a`
- 시험: `tests/test_budget.py` (28개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | profile max_calls로 총괄 호출을 포함해 pool·profile·목표별 상한을 검사한다. 목표 scope는 호출자가 넣지 않으면 검사되지 않는다. |
| 2 | 단위 범위 충족 | api_cost_cents·토큰·quota_used+단위를 환산 없이 분리하고, confirmed/estimated/unknown basis를 구분한다. unknown을 0으로 채우지 않는다. |
| 3 | 단위 범위 충족 | rejected로 신규 배치를 멈추고 late 보고를 정산해 over_limit로 표시한다. 문서는 외부 청구 절대 상한 비보장을 명시한다. |

차단 사유:
- 선행 AIK-005·019·020·023 미완료
- 실제 provider 사용량·청구·429·quota 응답 연결 없음
- quota 잔여량 검사가 호출당 최소 1단위 하한 가정에 의존

실행하지 않은 검증(NOT_RUN):
- Windows 기본 경로·msvcrt 잠금·두 프로세스 경합
- 실제 provider·실계정 사용량·청구 검증
- 대규모 처리량

## AIK-041

- 구현: src/aik_elastic의 ElasticityController가 VM ready 순서를 그대로 따라 AIK-033 PlacementPlanner로 배치하고, aik_lifecycle.GatedLeaseManager로만 lease를 확보한다(게이트 없는 manager는 UNGATED_LEASE_MANAGER로 거절). pool별 점유 lease 수(HOLDING_STATES)로 CAPACITY_EXHAUSTED·CONTROL_RESERVE 대기를 결정하고, control_reserve 슬롯만큼 worker 점유를 막는다. planner 호출 전에 pool·프로젝트 lifecycle gate를 확인해 pause 중 Attempt placement 소모를 막는다. dispatch되지 않은 reserved lease를 idle_seconds 뒤 반환하고, 완료/실패 뒤 정리는 LifecycleController.teardown에 위임한다.
- 검증: tests/test_elastic.py 7개가 실제 RuntimeStore·BudgetStore·PlacementPlanner·LifecycleGate·GatedLeaseManager·WorkerObserver와 주입 시계로 같은 프로젝트 직렬, 다른 프로젝트 제한 병렬·CAPACITY_EXHAUSTED, control reserve(max_parallel 2/1), pause 대기와 resume 후 grant, 미전송 reserved lease만 회수, 미해결 효과 시 teardown 미정리를 검사한다. LeaseManager.reserve만 grant double로 바꿔 예산 승인 경로는 이 시험에서 실행하지 않았다.
- 커밋: `4f85cf0`
- 시험: `tests/test_elastic.py` (7개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | VM ready 순서와 planner의 프로젝트 직렬·qualified route 제약을 따르는 제한 병렬 배정은 단위 시험했다. 복수 worker의 실제 동시 실행은 실제 provider·worker 프로세스 없이 검증하지 않았다. |
| 2 | 부분 | 유휴 미전송 lease 반환과 teardown 위임(evidence 검증·usage 정산·자체 생성 worker 종료 규칙)은 단위 시험했다. 실제 worker 프로세스 종료와 실제 usage 회수는 NOT_RUN이다. |
| 3 | 단위 범위 충족 | control_reserve로 pool의 worker 점유 상한을 max_parallel - n으로 제한한다. 실제 provider 용량이나 제어 경로 성공을 보장하지 않는 AIK 배정 상한이다. |

차단 사유:
- 실제 provider·worker 프로세스로 복수 worker 동시 실행 미검증
- planner와 reserve 사이 pause·경합 시 해당 Attempt는 VM이 새 Attempt를 줄 때까지 재배치 불가
- 선행 AIK-003 Windows 실기 검증 미완료

실행하지 않은 검증(NOT_RUN):
- 복수 worker 실제 동시 실행
- 실제 worker 프로세스 생성·종료
- 예산 승인 경로를 포함한 reserve 결합 시험
- Windows 실행

## AIK-042

- 구현: src/aik_validation에 CompletionView를 구현해 provider 관측·dispatch ACK·worker 완료 선언·독립 검사·VM validation ID·VM gate 투영·GoalVerdict ID·재작업 사유를 출처와 함께 분리 표시한다. completion_authority는 항상 vm이고 vm_verdict_recorded는 VM final_report의 goal_verdict_id로만 참이 된다. VM 응답은 기대 command의 실제 BridgeReceipt만 읽고 Attempt·프로젝트 불일치를 거절한다. IndependentChecker는 reviewer 역할과 worker와 다른 세션만 허용하고(NOT_INDEPENDENT) 정책 고정 DevTools 검증 명령을 실행해 PASS/FAIL/ORACLE_CHANGED를 append-only CheckJournal에 기록한다.
- 검증: tests/test_validation.py 9개가 합성 payload를 담은 실제 BridgeReceipt와 실제 governance 투영, 실제 자식 프로세스 DevTools(fake authority)로 상태 분리, 완료 선언·PASS·ACK가 VM 판정을 만들지 않음, Attempt·프로젝트 불일치 거절, 독립성 거절, oracle 변경 판정, 재검사 append-only와 tool call 충돌을 검사한다.
- 커밋: `dd7899e`, `739deb9`
- 시험: `tests/test_validation.py` (9개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 단위 범위 충족 | provider 관측·ACK·독립 검사·VM validation·gate·GoalVerdict를 서로 다른 필드와 provenance로 두고 어느 신호도 VM 판정으로 승격하지 않는다. |
| 2 | 부분 | reviewer 역할·별도 세션과 실행 provenance를 기록하고 자기 완료 선언을 승격하지 않는다. AGS 독립검토의 실제 실행과 실제 authority gateway 인가는 NOT_RUN이다. |
| 3 | 단위 범위 충족 | VM 응답을 재해석하지 않고 재작업 사유를 원문 순서로 회수하며 검사 명령도 기존 authority gateway를 거친다. |
| 4 | 부분 | 정책 고정 명령과 oracle 변경 감지(ORACLE_CHANGED)로 worker의 기준 변경을 통과시키지 않는 것은 단위 시험했다. 의도된 실패 뒤 VM 재작업/Goal Test를 거쳐 수정 bytes로 성공하는 실제 루프는 NOT_RUN이다. |

차단 사유:
- 실제 VM Goal Test·재작업 루프와의 결합 미검증
- AIK-081 A2 호환 AGS plugin 미확보로 AGS 독립검토 실행 불가
- 선행 AIK-031 hosted 경로의 실제 governed 실행 미검증

실행하지 않은 검증(NOT_RUN):
- 실제 VM이 반환한 BridgeReceipt와의 결합
- AGS 독립검토 실제 실행과 authority gateway reviewer 인가
- 의도된 실패 뒤 VM 재작업 루프
- Windows 실행

## AIK-043

- 구현: src/aik_readmodel에 non-authoritative ReadModel을 구현했다. source별 연속 cursor 적용, 같은 digest 중복 무시, 다른 digest의 CURSOR_CONFLICT·EVENT_ID_CONFLICT append-only 기록, gap 표시(stale)와 늦은 이벤트로 순서 역전 복원, event_log 재적용 rebuild와 projection_digest를 제공한다. refs 허용 키로 VM Goal/Task/Attempt·AIK placement/lease/dispatch·AGS workflow/session/peer/message·worker 주소를 연결하고, attempt_view는 attempt 고유 ID로만 확장한다. user_scope는 DB에 한 번 고정해 다른 범위 batch를 전부 거절한다. 어댑터는 실제 BridgeReceipt의 refs.history_cursor·ID·job.vm_status, lease, peer 메시지를 원문 그대로 옮긴다.
- 검증: tests/test_readmodel.py 10개가 합성 ID를 담은 실제 BridgeReceipt·VmRefs·JobObservation과 합성 lease·peer 이벤트로 attempt 연결, peer 전달 이력, 중복·충돌, gap·순서 역전, rebuild digest 동일성, user_scope 거절, append-only trigger를 검사한다.
- 커밋: `dd7899e`, `739deb9`
- 시험: `tests/test_readmodel.py` (10개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | VM·AGS·AIK ID를 refs와 links로 연결하는 projection은 단위 시험했다. 실제 VM History·AGS PEER 이벤트 수집기는 없다. |
| 2 | 단위 범위 충족 | event_log 재적용 rebuild가 같은 projection_digest를 만들고 DB meta·모든 view에 non-authoritative를 표시한다. |
| 3 | 부분 | AGS peer의 delivered/claimed/acked/result와 worker 주소 연결, 같은 user_scope 고정은 구현했다. 실제 AGS 로컬 PEER와 OS principal 확인은 NOT_RUN이다. |

차단 사유:
- 실제 VM History·AGS PEER 수집기 없음
- VM history_cursor처럼 receipt 사이에 cursor가 비는 source는 영구 gap(stale)으로 남음
- AIK-081 A2 호환 AGS plugin 미확보

실행하지 않은 검증(NOT_RUN):
- 실제 VM·AGS·provider 소스 연결
- GUI 연결
- OS 사용자 principal 확인
- Windows 실행

## AIK-084

- 구현: aik_workspace에 생성·열기·선택·보관·복원·재결속 수명주기, canonical root 규칙, VM project port, 영속 registry, shared_root 입력 digest 결속과 verify_task_binding을 구현했다.
- 검증: 임시 디렉터리와 fake VM port로 단위시험 대상을 작성했다. 한글·공백 경로, 실제 subprocess cwd, 재시작 복원, 이동·삭제·교체, 권한 상실, stale·변조·누락 입력의 효과 전 차단을 포함한다. 실제 VM status의 project root·not-found 분류를 고정 revision 소스 적합성 시험으로 확인. Windows CI에서 file ID 범위 결함을 발견·수정했고 이후 Windows CI에서 workspace 시험 통과.
- 커밋: `cbef3ce`, `ad223a5`, `554dc94`
- 시험: `tests/test_workspace.py` (32개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | workspace ID·canonical root·VM project를 결속하고 검증 문맥의 cwd를 subprocess에 적용했다. VM 생성 seam은 주입 가정이고 GUI·native cwd는 미연결이다. |
| 2 | 부분 | shared_root manifest digest를 TaskExecutionBinding에 결속하고 효과 전 차단을 구현했다. projection 미구현으로 검증 이후의 변경은 막지 못한다. |
| 3 | 부분 | 한글·공백·symlink·root 탈출·다른 workspace 참조를 Linux에서 시험 대상으로 삼았다. Windows의 reparse·대소문자·8.3 처리는 미검증이다. |
| 4 | 부분 | 재시작 복원과 이동·삭제·권한 상실별 재선택·재결속 action을 제공한다. 진행 중 Goal의 VM revision·인가 연결은 미구현이다. |

차단 사유:
- VM project 생성 공개 command·seam 부재
- 진행 중 Goal의 VM revision·인가 연결 미구현
- 선행 AIK-005(AIK-083 대기로 미승격)·AIK-031(todo) 미완료
- 실패 시 생성된 VM project가 고아로 남음, GUI·native 미연결

실행하지 않은 검증(NOT_RUN):
- Windows 실행(드라이브 판정·reparse·8.3·st_ino·ACL·LOCALAPPDATA)
- 실제 VM 설치물 status 응답 형식 확인
- VM project 생성 seam
- GUI·native 실행기 cwd 적용과 confirm_inputs_after_effect 연결
- 대규모 입력

## AIK-085

- 구현: aik_devtools에 고정 4개 도구(read/create/update/run_verification)를 구현했다. 효과 분류, gateway intent·receipt 연결, CAS 쓰기, 한도가 있는 검증 명령, oracle digest 고정을 포함한다.
- 검증: fake gateway, 실제 임시 파일, python -c 자식 프로세스로 단위시험 대상을 작성했다. 범위 거부, 재전송 비재실행, 예외 주입 중단, timeout·출력 잘림, oracle 변조 탐지를 포함하며 문서에는 실행 결과 기록이 없다. Windows CI에서 shell 분류 순서 결함을 발견·수정.
- 커밋: `f899a3e`, `1626974`, `42b7443`
- 시험: `tests/test_devtools.py` (28개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | 고정 도구·인자·cwd·timeout·출력 한도와 미지원 능력 목록을 구현했다. 실제 개발 worker(native/API)와는 연결하지 않았다. |
| 2 | 부분 | 인가·intent·receipt를 gateway에 위임한다. fake는 lease·만료를 검사하지 않고, DevTools는 request_digest·lease를 대조하지 않는다. |
| 3 | 부분 | 재전송 비재실행과 중단 후 관측·unknown 처리를 구현했다. 중단은 실제 kill이 아닌 예외 주입으로 재현했고, native와 API의 보장 범위 구분은 문서화했다. |
| 4 | 부분 | 문서가 직접 부분 충족이라고 밝힌다. 열거된 oracle의 변조 탐지만 보장하며, 열거되지 않은 판정 관여 파일과 native 직접 수정은 막지 못한다. |

차단 사유:
- 실제 AuthorityGateway·VM 효과 권위 extension과 결합하지 않음
- 선행 AIK-029·AIK-031·AIK-084 미완료(todo)
- 전체 live governed 경로는 AIK-086 범위
- 실제 개발 worker(native/API) 연결 부재
- 긴 검증 명령 중 lease 재확인 없음

실행하지 않은 검증(NOT_RUN):
- 실제 gateway·VM 효과 권위 연동
- Windows 실행(프로세스 종료·os.link·권한 비트·reparse·링크 수)
- 실제 worker native/API 연결
- 실제 프로세스 kill 기반 crash 재현
- AIK-086 live governed 경로

### AIK-085 추가 기록 — VM 설계 기준 효과 journal

- **구현:** VM 설계에는 tool call 단위 intent API가 없다. 그래서 `VmEffectAuthorityPort`를 AIK 소유 SQLite journal(`AikEffectJournal`)로 구현했다.
  - 확인된 execution binding과 running Attempt가 있을 때만 intent를 기록한다.
  - journal의 상태는 VM 판정이 아니다.
- **외부 효과:** `ExternalEffectSubmitter`가 Task 단위 typed `EffectIdentity` receipt를 VM `record_effect_receipt`에 제출한다.
  - VM 거절 코드는 메시지 앞머리에서 읽어 보존한다.
  - VM의 receipt ID(문자열) 응답을 받아들인다.
  - 반환 상태는 `target_reobservation_required`다. 효과 완료로 판정하지 않는다.
- **검증:**
  - `tests/test_effect_journal.py`(24개)가 세션 Linux 전체 회귀에서 PASS.
  - 독립 API 검토 R2 finding 중 F1~F5·F7·F8을 수정했다. F2는 문서로 명시했고, F6은 알려진 한계로 남겼다.
- **커밋:** `4dd7041`, `2f7bf25`
- **NOT_RUN:**
  - 실제 VM `EngineService` 연결. `EngineApplication`에 receipt wrapper가 없다.
  - 설치형 VM
  - Windows
  - 실제 외부 서비스

## 보충 근거: M2 결합 시험과 recovery 실제 adapter

PR #15(commit `004ae1f`·`5953027`)는 상태를 바꾸지 않고 아래 태스크에 근거를 보충한다. 근거 ID는 `EV-<태스크>-SUPP-M2INT-20261013`이다.

- 시험: `tests/test_m2_integration.py`(6개 시나리오), `tests/test_recovery_adapter.py`(8개). 세션 Linux 전체 회귀가 통과했고, GitHub Actions Ubuntu·Windows도 통과했다.
- 상세: [M2 결합 시험](../../../docs/m2-integration.ko.md), [recovery 문서](../../../docs/recovery.ko.md)의 "API 경로 effect port" 절.

| 태스크 | 주제 | 보충 내용 |
|---|---|---|
| AIK-039 | ToolEffectAdapter | 보충: aik_recovery.ToolEffectAdapter가 RecoveryReconciler의 effect port(inflight·mark_unknown·resend_result)를 실제 aik_dispatch.DispatchJournal tool event 위에 구현했다(commit 5953027, tests/test_recovery_adapter.py 8개, M2 결합 시험 시나리오 4). 기존 근거의 'fake port 시험뿐'을 API 경로에 한해 정정한다. hosted runtime 경로(AikEffectJournal) adapter와 실제 ApiAgentLoop.resume 재전송 결합은 NOT_RUN. 상태 변경 없음. |
| AIK-035 | open_tool_requests | 보충: DispatchJournal.open_tool_requests() 읽기 전용 조회 추가(commit 5953027). API 경로 등록은 dispatch 이벤트가 없어 recovery가 이 조회로 미반환 tool call을 찾는다. M2 결합 시험에서 실제 NativeDispatcher+GuardedAdapter의 known/unknown 분류, 재시작 뒤 recover()·중복 dispatch 무호출을 확인했다. 상태 변경 없음. |
| AIK-033 | M2 결합 시험 | 보충: commit 004ae1f·5953027(PR #15), tests/test_m2_integration.py 6개 시나리오가 이 모듈을 다른 M2 모듈의 실제 인스턴스와 한 흐름으로 연결해 PASS(세션 Linux 전체 회귀 OK). 외부 경계(provider·VM 응답·authority·worker port)만 fake이며 실제 provider·VM·AGS·Windows는 NOT_RUN. 모듈 사이 새 결함 없음. 상태 변경 없음. |
| AIK-034 | M2 결합 시험 | 보충: commit 004ae1f·5953027(PR #15), tests/test_m2_integration.py 6개 시나리오가 이 모듈을 다른 M2 모듈의 실제 인스턴스와 한 흐름으로 연결해 PASS(세션 Linux 전체 회귀 OK). 외부 경계(provider·VM 응답·authority·worker port)만 fake이며 실제 provider·VM·AGS·Windows는 NOT_RUN. 모듈 사이 새 결함 없음. 상태 변경 없음. |
| AIK-036 | M2 결합 시험 | 보충: commit 004ae1f·5953027(PR #15), tests/test_m2_integration.py 6개 시나리오가 이 모듈을 다른 M2 모듈의 실제 인스턴스와 한 흐름으로 연결해 PASS(세션 Linux 전체 회귀 OK). 외부 경계(provider·VM 응답·authority·worker port)만 fake이며 실제 provider·VM·AGS·Windows는 NOT_RUN. 모듈 사이 새 결함 없음. 상태 변경 없음. |
| AIK-037 | M2 결합 시험 | 보충: commit 004ae1f·5953027(PR #15), tests/test_m2_integration.py 6개 시나리오가 이 모듈을 다른 M2 모듈의 실제 인스턴스와 한 흐름으로 연결해 PASS(세션 Linux 전체 회귀 OK). 외부 경계(provider·VM 응답·authority·worker port)만 fake이며 실제 provider·VM·AGS·Windows는 NOT_RUN. 모듈 사이 새 결함 없음. 상태 변경 없음. |
| AIK-041 | M2 결합 시험 | 보충: commit 004ae1f·5953027(PR #15), tests/test_m2_integration.py 6개 시나리오가 이 모듈을 다른 M2 모듈의 실제 인스턴스와 한 흐름으로 연결해 PASS(세션 Linux 전체 회귀 OK). 외부 경계(provider·VM 응답·authority·worker port)만 fake이며 실제 provider·VM·AGS·Windows는 NOT_RUN. 모듈 사이 새 결함 없음. 상태 변경 없음. |
| AIK-042 | M2 결합 시험 | 보충: commit 004ae1f·5953027(PR #15), tests/test_m2_integration.py 6개 시나리오가 이 모듈을 다른 M2 모듈의 실제 인스턴스와 한 흐름으로 연결해 PASS(세션 Linux 전체 회귀 OK). 외부 경계(provider·VM 응답·authority·worker port)만 fake이며 실제 provider·VM·AGS·Windows는 NOT_RUN. 모듈 사이 새 결함 없음. 상태 변경 없음. |
| AIK-043 | M2 결합 시험 | 보충: commit 004ae1f·5953027(PR #15), tests/test_m2_integration.py 6개 시나리오가 이 모듈을 다른 M2 모듈의 실제 인스턴스와 한 흐름으로 연결해 PASS(세션 Linux 전체 회귀 OK). 외부 경계(provider·VM 응답·authority·worker port)만 fake이며 실제 provider·VM·AGS·Windows는 NOT_RUN. 모듈 사이 새 결함 없음. 상태 변경 없음. |

## 보충 근거: AGS PEER bridge

`src/aik_ags_peer`는 상태를 바꾸지 않고 아래 태스크에 근거를 보충한다. 근거 ID는 `EV-<태스크>-SUPP-AGSPEER-20261013`이다.

- 시험: `tests/test_ags_peer.py`(17개). 실제 AGS 2.9.0(`56fef8bd`) CLI 계약 시험을 포함하며, 세션 Linux에서 통과했다. CI에는 AGS checkout이 없어 실제 CLI 시험은 skip된다.
- 상세: [AGS PEER bridge 문서](../../../docs/ags-peer.ko.md).

| 태스크 | 주제 | 보충 내용 |
|---|---|---|
| AIK-043 | AGS PEER bridge | 보충: src/aik_ags_peer(AGS 2.9.0 세션 메시징 공개 CLI 계약 adapter)가 AIK worker 주소(aik-worker:<id>)와 AGS {host, sessionId}를 append-only AddressBook으로 연결하고 delivered/claimed/acked/result 단계를 from_peer_message 입력으로 내보낸다. tests/test_ags_peer.py 17개 PASS이며, 그중 실제 AGS 2.9.0(56fef8bd) CLI·node v24로 task→claim·ack→result 왕복과 실제 ReadModel peer_view를 확인했다. result는 AGS 유형이 아니라 AIK 규약 aik.peer.v1이다. 실제 worker 호스트 세션·Windows·OS principal 확인은 NOT_RUN. 상태 변경 없음. |
| AIK-025 | AGS PEER bridge | 보충: src/aik_ags_peer가 AGS 세션 메시징으로 총괄↔worker 메시지를 보내고 result를 claim한다(전달 불명은 send_unknown, 자동 재전송 없음, ACK는 완료 아님). 실제 AGS 2.9.0 CLI 계약 시험 PASS. aik_inbox 큐·wake와의 연결은 아직 없어 NOT_RUN. 상태 변경 없음. |

## AIK-086

- 구현: src/aik_governance_env가 VM(vibemarshal 32bb0f9) governance gate 입력 세 가지를 VM 계약 형식대로 만들고 검사한다. (1) model class 대응표(flowmarshal-governance-model-classes-v1)는 capability inventory의 관측 모델 이름만 허용해 생성·검사한다. (2) A2 profile 설정(flowmarshal-governance-a2-profile-v1)은 key/pin/state 위치로 resourceBindingDigest·pinSetDigest·freezeIdentity를 AGS canonicalJson과 같은 방식으로 계산해 조립하고, VM과 같은 코드로 검사한다. 개인키는 생성·저장·반환하지 않는다. (3) plugin root host-integration.json 구조와 closure_tree_digest는 VM preflight 공식과 같게 계산한다. 셋이 모두 READY일 때만 FLOWMARSHAL_GOVERNANCE_* 환경을 내준다.
- 검증: tests/test_governance_env.py 26개 PASS. 그중 실제 VM 계약 시험 3개가 있다: AIK가 조립한 A2 설정을 VM _load_a2_producer가 실제 Ed25519 임시 키쌍으로 수용하고 freezeIdentity 변조는 VM·AIK가 같은 코드로 거절함, AIK model 표를 VM load_model_classes가 같은 digest로 수용함, closure_tree_digest가 VM preflight 공식(sha256_bytes·_sha256_file)과 같음. 세션 Linux에서 VM 소스와 cryptography로 실행했으며 CI modules job에는 VM이 없어 skip(NOT_RUN)된다.
- 커밋: `PR #16 aik_governance_env`
- 시험: `tests/test_governance_env.py` (26개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 부분 | AIK가 VM gate에 공급할 governance 입력을 VM 계약대로 만들고 VM 수용을 실측했다. 고정 test-only route의 실제 governed 실행, 사용자 인가·budget/lease·AGS A2 서버 연결은 NOT_RUN이다. |
| 2 | 이 환경 검증 불가 | 필수 runtime·역할의 실제 model/effort·connection·provider job 대조에는 설치형 AGS·VM과 실계정이 필요하다. |
| 3 | 부분 | 도구 intent/receipt/결과 반환 경계의 재시작 판단은 aik_recovery.ToolEffectAdapter와 M2 결합 시험으로 저장소 안에서 검증했다. 실제 설치본에서의 crash 실측은 NOT_RUN이다. |
| 4 | 이 환경 검증 불가 | 역할별 계약 coverage와 대표 합성 목표 044의 구분은 실제 qualification 실행에서 판정한다. |

차단 사유:
- AIK-022 Windows 멀티벤더 인증 qualification 미완료
- AIK-081 AGS A2 plugin 미확보(host-integration.json·A2는 AGS 3.x 미병합)
- 실제 governed 실행·실계정 미실행

실행하지 않은 검증(NOT_RUN):
- 실제 AGS A2 서버·적합성 16검사
- steward 실행
- 설치형 route·역할·도구 실측
- Windows 실행

## AIK-022

- 구현: 설치형 Windows foundation 앱(Electron+frozen Python sidecar)에 네 번째 검사 카드 auth_vault를 추가했다. sidecar 명령 auth_vault_probe는 제품 모듈 aik_vault(Windows 사용자 DPAPI)·aik_connection·aik_auth_lifecycle·aik_runtime.paths.database_lock·aik_security를 실제로 써서 공개 합성 비밀로 10개 실패 경로를 실행하고 고정 코드만 반환한다: DPAPI 왕복·교체, 잠금 경합(VAULT_BUSY), 잘못된 참조(INVALID_REFERENCE), 잘린 blob(VAULT_CORRUPT), 외부 blob 모의(ACCESS_DENIED), 삭제된 참조(CREDENTIAL_NOT_FOUND), 인증 시계 만료 뒤 배치 거부, 자격 증명 삭제 뒤 배치 거부, 출력 redaction. 다른 사용자 프로필은 단일 사용자 세션에서 NOT_RUN_SINGLE_USER_SESSION으로 선언하고 통과로 주장하지 않는다. 출력에 합성 비밀이 있으면 FAIL(AUTH_VAULT_SECRET_LEAKED)이다. Windows handoff 키트(verify-installed-spike·run-validation·build_windows_handoff)는 auth_vault 카드를 필수로 요구하고 review_windows_client.py는 결과를 aik022_auth_vault로 판정한다.
- 검증: 세션 Linux: spike 시험 test_auth_vault.py(fake vault로 10개 기대 코드 일치, 비밀 비노출, 내부 실패 FAIL, 비Windows NOT_RUN)와 handoff·review 시험, node protocol 시험 PASS. GitHub Actions(PR #17 head f731b1e, merge e025f41): windows-foundation installed-spike job 115434838577(run 38437391572)이 Windows Server runner에서 미서명 설치본을 빌드·설치하고 실제 GUI의 auth_vault 카드 PASS를 요구하는 verify-installed-spike.ps1을 통과했다(Installed foundation-spike validation: PASS, 증거 아티팩트 11832983859 zip sha256 66eeb5c5c628cd6915c7c3284952bffbe5ec5f44bf7a6f30760814121dfcecd3). 같은 job의 spike 단위 시험에서 실제 Windows DPAPI 경로 시험도 실행된다. 증거 아티팩트 본문은 egress proxy 차단으로 내려받지 못해 job 로그 판정으로만 확인했다.
- 커밋: `PR #17 aik_spike.auth_vault`
- 시험: `spikes/windows-foundation/tests/test_auth_vault.py` (5개)

| 완료 기준 | 판정 | 근거 |
|---|---|---|
| 1 | 미충족 | 실제 API/구독 인증 경로의 Windows 등록·호출·회전·해제·재연결과 같은 벤더 두 연결 분리는 실계정이 필요하며 실행하지 않았다(NOT_RUN). 합성 probe는 이 기준을 충족하지 않는다. |
| 2 | 부분 | 잠금·손상된 참조·인증 만료·로그(출력) 제거·자격 증명 삭제를 합성 비밀로 실제 Windows Server DPAPI 설치본에서 검증했다. 다른 사용자 조건은 두 번째 Windows 계정이 없어 NOT_RUN이며 외부 blob은 모의로만 검증했다. Windows 11 표준 사용자 실기는 사용자 PC 실행 대기. |
| 3 | 부분 | probe는 auth/vault 실패 경로만 판정하고 governed 실행 준비 여부를 판정하지 않는다(handoff review에서 별도 항목). 필수 경로 matrix별 live 판정·관측 시각 기록은 실계정 실행 전이라 NOT_RUN이다. |

차단 사유:
- 실계정 인증 경로(기준 1) 미실행: 사용자 key 입력 UI·실행 필요
- 다른 Windows 사용자 프로필 조건 NOT_RUN(두 번째 계정 필요)
- Windows 11 표준 사용자 실기 결과 대기(AIK-003 handoff와 같은 실행)

실행하지 않은 검증(NOT_RUN):
- 실계정 등록·호출·회전·해제·재연결
- 같은 벤더 두 연결·외부 로그인 변경
- 다른 Windows 사용자 프로필 DPAPI 거부
- Windows 11 표준 사용자 실기
- matrix revision별 live qualification

## 보충 근거: recovery 재전송과 ApiToolLoop.resume 결합

`aik_recovery.LoopResendScheduler`는 상태를 바꾸지 않고 아래 태스크에 근거를 보충한다. 근거 ID는 `EV-<태스크>-SUPP-RESEND-20261014`이다.

- 시험: `tests/test_resend_resume.py`(8개), `tests/test_recovery_adapter.py`(9개). 세션 Linux에서 통과했다.
- 상세: [복구 문서의 재전송 스케줄러 절](../../../docs/recovery.ko.md).

| 태스크 | 주제 | 보충 내용 |
|---|---|---|
| AIK-039 | recovery 재전송 결합 | 보충: aik_recovery.LoopResendScheduler가 ToolEffectAdapter의 resend를 실제 ApiToolLoop.resume에 연결했다(request당 resume 1회, returned 확인 뒤에만 성공, 미전달은 RESEND_NOT_DELIVERED 등 고정 코드로 reconciler failed 기록). adapter는 같은 request의 resume으로 이미 returned된 sibling을 다시 넘기지 않는다. tests/test_resend_resume.py 8개(실제 loop crash 뒤 새 reconciler로 receipt 재전송 1회·denied·두 call·intent sibling 차단·새 DevTools REPLAY_MISMATCH·context 없음)와 adapter 시험 1개 추가, 세션 Linux PASS. 영속 replay 저장소·resume context 보관소·실제 프로세스 kill·Windows는 NOT_RUN. 상태 변경 없음. |
| AIK-086 | 재시작 경계 결합 | 보충(기준 3): 도구 intent/receipt/결과 반환 경계에서 재시작 뒤 recovery가 실제 ApiToolLoop.resume으로 같은 결과를 1회 재전송하고(효과 재실행 없음), 전달하지 못한 경우 applied가 아닌 failed로 남김을 tests/test_resend_resume.py로 저장소 안에서 확인했다. 새 프로세스에서는 replay 기억이 없어 실패로 닫힌다(멱등 create_file은 재관측으로 receipt가 한 번 더 전달됨, 고정 시험). 설치본 crash 실측·governed 실행은 NOT_RUN. 상태 변경 없음. |
