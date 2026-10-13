# AIK-004 — 데스크톱 IPC 경계 (부분 구현)

## 구현 범위

AIK-004의 프로세스 경계·버전형 IPC·명령 라우터 중 **renderer와 Electron main 사이의 제한된 읽기 경계**를 구현했다. 제품 GUI/VM/AGS/시크릿 브로커의 전체 연결을 완료한 것은 아니다.

- src/aik_ipc/desktop.cjs: 허용 명령 2개만 노출하는 버전 1 라우터. 엄격한 5필드 요청과 빈 payload, UUID v4, workspace 일치, main frame sender identity, 응답의 고정 필드를 확인한다.
- src/aik_ipc/preload.cjs: sandboxed preload에서 window.aikDesktop.handshake()와 status(workspaceId)만 노출한다. Node 내장 모듈을 require하지 않고 secure origin의 Web Crypto로 UUID를 생성한다.
- protocol.handshake: 선택된 workspace와 무관한 unbound 요청, 결과는 IPC 연결 계약에만 한정하고 backend_state=NOT_QUALIFIED를 명시한다.
- system.status: Electron main이 바인딩한 활성 workspace와 일치할 때만 trusted native callback을 호출한다. callback 미설정은 BACKEND_UNAVAILABLE이며, backend의 예외·원시 비밀·추가 결과 필드는 renderer에 전달하지 않는다.
- 이전 request_id 재전송 거절(기본 10분/최대 4096건), 최대 동시 8개, 비동기 처리 중 workspace 전환·sender 소멸 시 응답 거부. 이는 단기 중복 방어이며 durable exactly-once 실행 승인은 아니다.

## 프로세스·권한 경계

| 프로세스/경로 | OS principal · trust profile | 권한·보장 |
| --- | --- | --- |
| Electron sandboxed renderer | Windows 동일 로그인 사용자, 비신뢰 UI | 제한된 handshake/status 요청. 임의 shell, 파일시스템, raw credential API 노출 없음 |
| Electron main + desktop router | 동일 로그인 사용자, trusted native host | webContents, main frame, 앱 URL, 활성 workspace를 독립 확인한 뒤 호출 |
| Python sidecar·총괄·worker·vault broker(후속 연결) | trusted same-user native 경로; 실행 주체별 실제 권한 별도 확인 필요 | 이번 구현에서 **미연결**. 앱 IPC 제한을 별도 OS user/프로세스 격리라고 주장하지 않음 |
| 제공자 API 제한 worker(후속 연결) | provider별 최소 권한 trust profile | 이번 구현에서 **미연결**. native 명령과 API 기반 분석 명령을 혼동하지 않음 |

## 응답 계약

요청은 protocol_version, request_id, workspace_id, command, payload만 허용한다. 프로토콜 버전 불일치에는 INCOMPATIBLE_PROTOCOL과 supported_versions=[1]을 반환하며 임의 다운그레이드를 허용하지 않는다. 잘못된 sender, command, workspace, 빈 payload 이외의 데이터는 명시적인 정적 오류 코드로 거절한다. backend 상태는 state=READY/DEGRADED/BLOCKED 및 director_state=NOT_CONFIGURED/STOPPED/RUNNING/UNAVAILABLE의 정확한 두 필드로 제한한다. 백엔드 오류 문자열·스택은 외부로 내보내지 않는다.

Electron main에서 적용할 때는 CHANNEL에 makeRendererHandler({router, getWindow, getWorkspaceId, appUrl})을 등록하며 세 getter와 readStatus callback은 **반드시 native main 소유**다. renderer에서 전달받으면 안 된다. preload.cjs를 packaging 대상에 포함해야 한다. AIK-002 설치 스파이크의 기존 고정 프로브 IPC를 실제 제품처럼 자동 변경하지 않았다.

## 검증 및 미충족 범위

로컬 Node 22.16.0에서 실행: node --test tests/test_desktop_ipc.cjs — 10/10 PASS, 실패·스킵 0개. 테스트는 발신자/외부 origin, subframe, workspace 불일치·경합, 프로토콜 오류, 허용되지 않은 command, accessor/추가 필드, 시크릿 누출, preload 제한, 재전송·동시성 경계를 확인한다. Windows Server 2022와 Ubuntu 24.04의 Node 22 전용 CI를 별도로 구성했다.

**아직 검증하지 않은 것:** 실제 제품 Electron main/sidecar와의 packaging·설치형 IPC 왕복, 사용자별 Windows 11 실사용 환경, 네이티브 실행/credential 접근의 OS principal 격리, AGS/VM 통합 및 AIK-083의 제품 인수 계약. 선행 AIK-083은 아직 미완료이며 기존 완료 기준·의존성을 바꾸지 않는다. 본 구현을 AIK-004 전체 완료로 취급하지 않는다.

## 원장 주의

정본은 planning/backlog.sqlite3다. 이 구현에서는 바이너리 원장을 무검증으로 갱신하지 않으며, 관련 근거는 evidence/desktop-ipc/2026-10-13/README.ko.md에 보관한다. 원장의 AIK-004 todo 상태·선행 의존성을 그대로 유지한다.
