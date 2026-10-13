# AIK 로컬 supervisor — AIK-006 (부분 구현)

`src/aik_supervisor/`는 사용자·workspace·component(기본 `director`, sidecar는 별도 component 이름)마다 런타임 owner를 하나로 제한한다. GUI 창의 수명과 런타임 수명을 분리하고, 두 번째 실행은 기존 owner의 재연결 endpoint에 attach한다. 이 모듈은 표준 라이브러리와 `aik_runtime`만 사용한다.

## 계약

- **인스턴스 잠금**: `<state_root>/<workspace_id>/<component>.instance.lock`에 OS 파일 잠금을 건다. POSIX는 `fcntl.flock`, Windows는 `msvcrt.locking`을 쓴다. 프로세스가 죽으면 OS가 잠금을 해제한다. PID 파일이나 PID 존재 여부로 생존을 판정하지 않는다. 잠금 파일은 삭제하지 않는다.
- **기본 위치**: Windows는 `%LOCALAPPDATA%\AIK\supervisor`, 개발용 POSIX fallback은 `$XDG_STATE_HOME/aik/supervisor` 또는 `~/.local/state/aik/supervisor`다. 경로는 `aik_runtime.paths.validate_database_path`로 검사한다. 따라서 상대 경로, 저장소·Git 내부, 개발 원장 이름, symlink·hardlink, Windows 장치 이름은 거절된다.
- **workspace_id**: `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` 형식만 허용한다. component는 `[a-z][a-z0-9-]{0,31}` 형식이다.
- **재연결 endpoint**: `<component>.endpoint.json`은 같은 디렉터리의 임시 파일에 쓰고 fsync한 뒤 `os.replace`로 원자적으로 게시한다. 필드는 `format_version`(1), `instance_id`, `workspace_id`, `component`, `pid`, `started_at`(UTC), `endpoint{kind,address}`로 고정한다. 그 외 필드가 있으면 `InvalidEndpoint`로 거절한다. 비밀 값을 담는 필드는 없다. `pid`는 정보 표시용이며 생존 판정에 쓰지 않는다.
- **endpoint 종류**: Windows는 `named-pipe`(`\\.\pipe\aik-<component>-<workspace>-<instance_id>`)를 쓴다. POSIX 개발 환경은 `unix-socket`(절대 경로)을 쓴다. 네트워크 주소는 허용하지 않는다. 소켓·pipe 서버 구현과 handshake는 이번 범위 밖이다.
- **launch**: `launch(workspace_id, store=..., state_root=..., attach_timeout=5.0)`은 잠금을 얻으면 `Supervisor`(role `owner`)를 반환한다. 잠금이 이미 잡혀 있으면 유효한 endpoint를 읽어 `Attachment`(role `attached`)를 반환하며, 새 인스턴스를 만들지 않는다. 기한 안에 endpoint가 없으면 `SupervisorUnavailable`로 끝난다.
- **stale 정리**: 잠금을 얻은 owner는 남아 있는 endpoint를 죽은 owner의 기록으로 판단해 삭제한다. 이후 새 기록으로 교체하고 `stale_endpoint_replaced`와 `previous_instance_id`를 보고한다.

## 상태 전이

창 상태(`WINDOW_OPEN`/`WINDOW_CLOSED`)와 수명 상태(`RUNNING`/`PAUSED`/`SHUTTING_DOWN`/`STOPPED`)는 서로 독립된 축이다.

| 동작 | 이전 수명 상태 | 결과 | 신규 배치(`admit_placement`) | 진행 중 job |
| --- | --- | --- | --- | --- |
| `close_window()` | RUNNING/PAUSED | 창 → WINDOW_CLOSED, 수명 상태 유지 | 변화 없음 | 유지 |
| `open_window()` | RUNNING/PAUSED | 창 → WINDOW_OPEN | 변화 없음 | 유지 |
| `pause()` | RUNNING/PAUSED | PAUSED | 거절(`PlacementRejected`) | 유지 |
| `resume()` | RUNNING/PAUSED | RUNNING | 허용 | 유지 |
| `shutdown()` | RUNNING/PAUSED | SHUTTING_DOWN → STOPPED, endpoint 삭제, 잠금 해제 | 거절 | 삭제·변경하지 않음, 보고서에 ID 기록 |
| 모든 창/일시정지 동작 | SHUTTING_DOWN/STOPPED | `SupervisorError` | 거절 | — |

`shutdown()`을 반복 호출하면 첫 번째 보고서를 그대로 반환한다.

## 진행 중 job 보존

`aik_runtime.RuntimeStore`의 `Lease`를 사용하며, 스키마는 변경하지 않는다. `reserved`·`active`·`draining`·`unknown` 상태의 lease를 진행 중 job으로 본다.

- 종료 시 이 lease들을 삭제하거나 상태를 바꾸지 않는다. 그 ID를 `ShutdownReport.in_flight_lease_ids`로 보고한다.
- 재시작한 owner는 같은 조건으로 스캔한 결과를 `observation_targets`로 노출한다.
- lease 상태 전이·fencing·실제 provider 관측은 AIK-033/034의 범위다. 이 모듈이 관측 대상을 `unknown`으로 바꾸면 그 권한을 침범하므로 저장값을 변경하지 않았다.
- 종료 중 스캔에 실패하면 `scan_complete=False`로 보고한다. 이는 진행 중 job이 "없음"이 아니라 "알 수 없음"을 뜻한다.
- 시작 중 스캔에 실패하면 owner가 되지 않고 잠금을 해제한다(fail-closed).
- `admit_placement`는 RUNNING 상태에서만 `Placement`를 저장한다. 저장 성공은 실행 인가가 아니다.

## 검증 범위

```bash
python -m unittest tests/test_supervisor.py -v
```

시험 항목은 다음과 같다.

- 실제 자식 프로세스 2개를 동시에 기동했을 때 owner는 하나이고 같은 instance_id에 attach한다.
- 창을 닫았다 다시 열 때 같은 인스턴스에 attach한다.
- 실제 owner 프로세스를 강제 종료(kill, POSIX에서 SIGKILL)한 뒤 재기동하면 잠금 자동 해제를 확인하고 stale endpoint를 교체한다.
- 일시정지·종료 시 신규 배치를 거부하고 진행 중 lease를 보존하며, 재시작 시 관측 대상으로 표시한다.
- 잘못된 workspace_id·component·상대 경로·저장소 내부 경로를 거부한다.
- endpoint의 고정 필드를 확인하고 비밀 키워드가 없음을 검사한다.
- Windows `msvcrt` 분기를 mock으로 검사한다.

모든 데이터는 OS 임시 폴더의 합성 값이며, provider·credential·네트워크를 사용하지 않는다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

## 미검증 항목 (NOT_RUN)

- 실제 Windows 11에서의 `msvcrt.locking` 동작, `LOCALAPPDATA` 경로, 프로세스 강제 종료 후 잠금 해제.
- named pipe·unix socket 서버, 접근 제어(사용자별 ACL, `FILE_FLAG_FIRST_PIPE_INSTANCE`), 재연결 handshake. unix socket 경로 길이 제한(약 108바이트)도 서버 구현 시 확인해야 한다.
- Electron GUI 창 이벤트와의 실제 연결, 사용자 세션 경계(다른 Windows 사용자·RDP 세션)에서의 격리.
- 잔여 경쟁 창: 이전 owner가 죽은 직후 새 owner가 잠금을 얻고 stale endpoint를 지우기 전의 짧은 순간, attach 측이 이전 기록을 읽을 수 있다. 최종 확인은 endpoint handshake에서 instance_id를 대조해야 하며, 이 handshake는 아직 구현되지 않았다.

이 문서와 시험 결과는 AIK-006의 완료 판정이 아니다. 장부 상태는 변경하지 않는다.
