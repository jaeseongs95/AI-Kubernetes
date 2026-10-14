# AIK-004 — 데스크톱 IPC 경계 (부분 구현)

renderer와 Electron main 사이의 제한된 읽기 IPC를 제공한다. 허용하는 명령은 `protocol.handshake`, `system.status`와 AIK-045가 더한 읽기 전용 `engine.info`·`readmodel.sources` 네 개다. 제품 GUI·VM/AGS·vault broker 전체 연결을 완료한 상태는 아니다. 데스크톱 셸 연결은 [desktop-shell](desktop-shell.ko.md)에 있다.

## 요청·응답의 소유권

`src/aik_ipc/desktop.cjs`는 정확한 다섯 요청 필드, 빈 payload, UUID v4, workspace, 앱 window/main frame/URL을 검증한다. 검증 직후 원시 객체를 버리고 동결한 primitive snapshot을 사용한다. 응답도 명령별로 정해진 필드만 하나씩 복사·동결해 반환한다(`system.status`는 `state`·`director_state`, `engine.info`는 엔진 버전·플랫폼 family·상태 저장소·read model 상태 등 8개, `readmodel.sources`는 source별 cursor·gap·conflict·stale). `stale`과 `open_gaps`가 서로 맞지 않거나 `authoritative`가 false가 아닌 응답은 `INVALID_RESPONSE`다. 원본이 비동기 처리 중 또는 반환 후 변경되어도 검증 결과에 반영되지 않는다.

Proxy·accessor·추가 필드·사용자 정의 thenable을 거절하며, 요청의 `toJSON`을 실행하지 않는다. backend 오류는 고정 코드/문구로 바꾸고 원시 예외·stack·민감 필드를 반환하지 않는다. 잘못된 protocol version은 `INCOMPATIBLE_PROTOCOL`과 `supported_versions=[1]`로 표시한다. main의 reader가 `BackendError("BACKEND_INCOMPATIBLE")`를 던지면 엔진과 앱의 버전 불일치로 그대로 전달한다. 다른 예외는 모두 `BACKEND_UNAVAILABLE`이다.

앱 문서는 `aik://app/index.html`이며, 같은 문서의 in-page fragment(`#/overview` 같은 hash route)는 같은 발신자로 인정한다. query string·다른 경로·다른 origin은 계속 `UNTRUSTED_SENDER`다.

## 응답 기한과 물리적 동시성

`new DesktopRouter({readStatus, requestTimeoutMs: 5000})`은 비동기 상태 조회를 기본 5초에 `BACKEND_TIMEOUT`으로 종료한다. 설정 범위는 1~60,000ms다. `readStatus(workspaceId, {signal, requestId})`의 두 번째 인자는 동결된 문맥이며, 기한이 지나면 AbortSignal을 알린다. 기존 한 인자 callback도 동작한다.

**응답 타임아웃은 실제 backend 작업 종료가 아니다.** abort를 무시한 Promise는 실제로 resolve/reject될 때까지 동시성 슬롯과 중복 방어 기록을 점유한다. 이때 신규 상태 조회는 `ROUTER_BUSY`로 차단되며, 늦은 결과/예외는 성공 응답으로 바뀌거나 원문으로 노출되지 않는다. 재전송 저장 공간이 남아 있다면 handshake는 backend 포화 상태에서도 응답하지만 `backend_state=NOT_QUALIFIED`를 유지한다.

기한은 이벤트 루프가 실행되는 비동기 대기에 적용한다. 동기 무한 루프를 선점하거나 native 프로세스를 강제 종료하지 않는다. 멈춘 backend의 교체/재시작은 supervisor 작업이며, 이 모듈이 그 권한을 만들지 않는다.

## 재전송과 범위

UUID 대소문자는 같은 request identity로 정규화한다. 미종료 요청은 보관 시간이 지나도 삭제하지 않으며, 실제 작업 종료 뒤 기본 10분간 replay fence를 유지한다. 시계 오류/역행은 기록을 조기에 삭제하는 근거가 되지 않는다. 기본 동시 상태 조회는 8개, replay 기록은 최대 4096개다. 전체 보관 한도는 65,536개, 설정 가능한 보관 시간은 1초~24시간이다.

비동기 처리 후 원래 window/main frame과 현재 workspace를 다시 확인한다. 다른 window, 파괴된 frame 또는 다른 활성 workspace에는 상태를 반환하지 않는다. 같은 workspace ID를 사용하는 동안 권한이 바뀌는 경우의 revision/인가 검사는 별도 제품 binding에 필요하다. 메모리 replay fence를 재시작 내구성이나 외부 효과 exactly-once 보장으로 해석하지 않는다.

## 프로세스·권한 경계

| 경로 | 책임 및 실제 범위 |
|---|---|
| sandboxed renderer | `preload.cjs`의 handshake·status·engineInfo·readmodelSources만 호출. Node·shell·전체 filesystem·raw secret API 없음 |
| trusted Electron main | 실제 webContents/frame/URL/활성 workspace와 readStatus callback 소유 |
| Python·총괄·worker·vault | 이번 모듈에서 제품 전체 미연결. 동일 Windows 사용자 native 경로의 OS 권한은 IPC 제한과 별개 |

설치 스파이크의 기존 프로브 IPC를 제품 IPC가 설치됐다는 증거로 사용하지 않는다. 이 라이브러리는 실제 제품 Electron main에 등록하고 preload를 패키징한 뒤 설치형 검증이 추가로 필요하다.

## 검증·원장

```bash
node --test tests/test_desktop_ipc.cjs tests/test_desktop_ipc_lifecycle.cjs tests/test_desktop_ipc_queries.cjs
python tools/backlog.py validate
```

기존 10개 테스트와 신규 25개 테스트를 실행한다. 초기 소스에 대한 8개 실패 반례와 수정 후 실행은 [근거 기록](../evidence/desktop-ipc/2026-10-13-lifecycle/README.ko.md)에 구분한다.

정본 `planning/backlog.sqlite3`에는 AIK-004의 구현 근거와 상태 이력을 기록하고, 미완료 AIK-083 및 제품 통합 조건 때문에 **blocked**로 유지한다. 다른 태스크·완료 기준·의존성은 변경하지 않는다. 단위시험 통과는 Windows 11 설치형 제품·OS principal 격리·VM/AGS 통합의 완료가 아니다.
