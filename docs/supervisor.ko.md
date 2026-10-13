# AIK 로컬 supervisor — AIK-006 (부분 구현)

`src/aik_supervisor/`는 사용자·workspace·component(기본 `director`, sidecar는 별도 component 이름)마다 런타임 owner를 하나로 제한한다. GUI 창의 수명과 런타임 수명을 분리하고, 두 번째 실행은 기존 owner의 재연결 endpoint에 attach한다. 이 모듈은 표준 라이브러리와 `aik_runtime`만 사용한다.

## 계약

- **인스턴스 잠금**: `<state_root>/<workspace_id 소문자>/<component>.instance.lock`에 OS 파일 잠금을 건다. POSIX는 `fcntl.flock`, Windows는 `msvcrt.locking`을 쓴다. 프로세스가 죽으면 OS가 잠금을 해제한다. PID 파일이나 PID 존재 여부로 생존을 판정하지 않는다. 잠금 파일은 삭제하지 않는다.
- **기본 위치**: Windows는 `%LOCALAPPDATA%\AIK\supervisor`, 개발용 POSIX fallback은 `$XDG_STATE_HOME/aik/supervisor` 또는 `~/.local/state/aik/supervisor`다. 경로는 `aik_runtime.paths.validate_database_path`로 검사한다. 따라서 상대 경로, 저장소·Git 내부, 개발 원장 이름, symlink·hardlink, Windows 장치 이름은 거절된다.
- **workspace_id**: `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` 형식만 허용한다. ASCII 대소문자를 구분하지 않는다. `WS-alpha`와 `ws-alpha`는 모든 OS에서 같은 workspace이며, 같은 소문자 디렉터리·잠금을 쓰고 서로에게 attach한다. endpoint 기록의 `workspace_id`에는 owner가 받은 원래 표기가 남는다. component는 `[a-z][a-z0-9-]{0,31}` 형식이다.
- **재연결 endpoint**: `<component>.endpoint.json`은 같은 디렉터리의 임시 파일에 쓰고 fsync한 뒤 `os.replace`로 원자적으로 게시한다. 필드는 `format_version`(1), `instance_id`, `workspace_id`, `component`, `pid`, `started_at`(UTC), `endpoint{kind,address}`로 고정한다. 그 외 필드가 있으면 `InvalidEndpoint`로 거절한다. JSON이 아니거나, 지나치게 깊게 중첩되었거나(파서 재귀 한도 초과), 읽기 중 I/O 오류가 나는 기록도 `InvalidEndpoint`다. 비밀 값을 담는 필드는 없다. `pid`는 정보 표시용이며 생존 판정에 쓰지 않는다.
- **endpoint 종류**: Windows는 `named-pipe`(`\\.\pipe\aik-<component>-<workspace>-<instance_id>`)를 쓴다. POSIX 개발 환경은 `unix-socket`(절대 경로)을 쓴다. 네트워크 주소는 허용하지 않는다. 소켓·pipe 서버 구현과 handshake는 이번 범위 밖이다.
- **launch**: `launch(workspace_id, store=..., state_root=..., attach_timeout=5.0)`은 잠금을 얻으면 `Supervisor`(role `owner`)를 반환한다. 잠금이 이미 잡혀 있으면 유효한 endpoint를 읽어 `Attachment`(role `attached`)를 반환하며, 새 인스턴스를 만들지 않는다. 기한 안에 endpoint가 없으면 `SupervisorUnavailable`로 끝난다.
- **attach_timeout 제약**: owner는 잠금을 얻은 뒤 시작 스캔을 마치고서야 endpoint를 게시하고, 종료 시에는 endpoint를 먼저 철회한 뒤 종료 스캔을 마치고 잠금을 해제한다. 두 스캔은 `RuntimeStore.transaction()`을 거치므로 DB 크기에 비례하는 사본 검사와 runtime 저장소 잠금 대기(`RuntimeStore` timeout, 기본 5초)를 포함한다. 이 구간에 실행된 두 번째 launch는 endpoint도 잠금도 얻지 못한다. 대기가 `attach_timeout`을 넘으면 owner가 정상 기동·종료 중이더라도 `SupervisorUnavailable`을 받는다. 호출자는 `attach_timeout`을 runtime 저장소 timeout과 예상 스캔 시간보다 크게 잡거나 재시도해야 한다. 기동 중 상태를 별도로 알리는 기능은 없다.
- **stale 정리**: 잠금을 얻은 owner는 남아 있는 endpoint(손상된 기록 포함)를 죽은 owner의 기록으로 판단해 삭제한다. 이후 새 기록으로 교체하고 `stale_endpoint_replaced`와 `previous_instance_id`(손상 기록이면 `None`)를 보고한다.

## 상태 전이

창 상태(`WINDOW_OPEN`/`WINDOW_CLOSED`)와 수명 상태(`RUNNING`/`PAUSED`/`SHUTTING_DOWN`/`STOPPED`)는 서로 독립된 축이다. 창을 닫아도 RUNNING이면 `admit_placement`는 계속 허용된다.

| 동작 | 이전 수명 상태 | 결과 | 신규 배치(`admit_placement`) | 진행 중 job |
| --- | --- | --- | --- | --- |
| `close_window()` | RUNNING/PAUSED | 창 → WINDOW_CLOSED, 수명 상태 유지 | 변화 없음 | 유지 |
| `open_window()` | RUNNING/PAUSED | 창 → WINDOW_OPEN | 변화 없음 | 유지 |
| `pause()` | RUNNING/PAUSED | PAUSED | 거절(`PlacementRejected`) | 유지 |
| `resume()` | RUNNING/PAUSED | RUNNING | 허용 | 유지 |
| `shutdown()` | RUNNING/PAUSED | SHUTTING_DOWN → 자기 endpoint 철회 → 진행 중 lease 스캔 → 잠금 해제 → STOPPED | 거절 | 삭제·변경하지 않음, 보고서에 ID 기록 |
| 모든 창/일시정지 동작 | SHUTTING_DOWN/STOPPED | `SupervisorError` | 거절 | — |

`shutdown()`을 반복 호출하면 첫 번째 보고서를 그대로 반환한다. endpoint 철회가 예외로 실패하면 스캔·잠금 해제·STOPPED 전이는 수행하지만 예외가 전파되고 보고서는 저장되지 않는다.

종료 시 endpoint를 스캔보다 먼저 철회한다. 따라서 종료 스캔 중에 GUI를 다시 열면 죽어가는 인스턴스에 attach하지 않고, 잠금 해제를 기다렸다가 새 owner가 된다(대기는 `attach_timeout` 이내).

**일시정지와 배치 중지의 범위**:

- 수명 상태는 프로세스 메모리에만 있다. `pause()` 후 앱을 종료하고 재시작하면 새 owner는 `RUNNING`으로 시작한다. 일시정지의 영속화는 이번 범위가 아니다.
- 신규 배치 거절은 `Supervisor.admit_placement` 경로에만 적용된다. 호출자가 같은 `RuntimeStore`에 `save(Placement(...))`를 직접 호출하면 수명 상태와 무관하게 저장된다. 배치 경로는 반드시 `admit_placement`를 거쳐야 한다.

## 진행 중 job 보존

`aik_runtime.RuntimeStore`의 `Lease`를 사용하며, 스키마는 변경하지 않는다. `reserved`·`active`·`draining`·`unknown` 상태의 lease를 진행 중 job으로 본다.

- 종료 시 이 lease들을 삭제하거나 상태를 바꾸지 않는다. 그 ID를 `ShutdownReport.in_flight_lease_ids`로 보고한다.
- 재시작한 owner는 같은 조건으로 스캔한 결과를 `observation_targets`로 노출하고, 스캔 수행 여부를 `observation_scan_complete`로 노출한다.
- lease 상태 전이·fencing·실제 provider 관측은 AIK-033/034의 범위다. 이 모듈이 관측 대상을 `unknown`으로 바꾸면 그 권한을 침범하므로 저장값을 변경하지 않았다.
- 종료 중 스캔에 실패하면 `scan_complete=False`로 보고한다. 이는 진행 중 job이 "없음"이 아니라 "알 수 없음"을 뜻한다.
- **`store=None`**: 스캔할 대상이 없으므로 시작 시 `observation_scan_complete=False`(`observation_targets=()`), 종료 시 `scan_complete=False`로 보고한다. 빈 목록을 "진행 중 job 없음 확인"으로 해석하지 않는다. store가 없는 owner는 `admit_placement`도 `SupervisorError`로 거절한다.
- 시작 중 스캔에 실패하면 owner가 되지 않고 잠금을 해제한다(fail-closed). endpoint도 게시하지 않는다.
- `admit_placement`는 RUNNING 상태에서만 `Placement`를 저장한다. 저장 성공은 실행 인가가 아니다.
- **스캔 범위는 store 전체다.** `Lease`·`Placement`에는 workspace 필드가 없으므로 이 모듈은 workspace별로 거를 수 없다. 기본 `RuntimeStore()`는 사용자당 하나다. 여러 workspace가 같은 store를 공유하면 `observation_targets`와 `ShutdownReport`에 다른 workspace의 lease가 포함된다. 결과를 workspace 단위 조치에 쓰려면 workspace마다 별도 store 경로를 써야 한다. 공유 store 거절은 구현하지 않았으며(식별 수단 없음), 해당 결정은 AIK-033/034 연결 시 필요하다.

## 검증 범위

```bash
python -m unittest tests/test_supervisor.py -v
```

시험 항목은 다음과 같다.

- 실제 자식 프로세스 2개를 동시에 기동했을 때 owner는 하나이고 같은 instance_id에 attach한다.
- 창을 닫았다 다시 열 때 같은 인스턴스에 attach한다(같은 프로세스 호출, 그리고 별도 자식 프로세스에서의 attach).
- 실제 owner 프로세스를 강제 종료(kill, POSIX에서 SIGKILL)한 뒤 재기동하면 잠금 자동 해제를 확인하고 stale endpoint를 교체한다. store를 쓰는 owner를 강제 종료한 뒤 재기동하면 active lease가 `observation_targets`에 나타나고 lease는 변경되지 않는다.
- 일시정지·종료 시 신규 배치를 거부하고 진행 중 lease를 보존하며, 재시작 시 관측 대상으로 표시한다. 창이 닫힌 RUNNING 상태에서는 배치를 허용한다.
- `store=None`이면 시작·종료 스캔을 "완료"로 보고하지 않는다.
- 종료 스캔 중에는 endpoint가 이미 철회되어 재실행이 attach하지 않는다.
- 종료 스캔 실패 시 `scan_complete=False`, 시작 스캔 실패 시 `SupervisorError`와 잠금 해제·endpoint 미게시를 확인한다(저장소 오류는 mock으로 주입).
- 지나치게 깊은 중첩·파서 재귀 오류 기록을 `InvalidEndpoint`로 분류하고 새 owner가 교체한다.
- 대소문자만 다른 workspace ID가 하나의 owner를 공유한다.
- 잘못된 workspace_id·component·상대 경로·저장소 내부 경로를 거부한다.
- endpoint의 고정 필드를 확인하고 비밀 키워드가 없음을 검사한다.
- Windows `msvcrt` 분기를 mock으로 검사한다.

모든 데이터는 OS 임시 폴더의 합성 값이며, provider·credential·네트워크를 사용하지 않는다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

## 미검증 항목 (NOT_RUN)

- 실제 Windows 11에서의 `msvcrt.locking` 동작, `LOCALAPPDATA` 경로, 프로세스 강제 종료 후 잠금 해제, NTFS에서의 대소문자 무시 동작.
- Windows 공유 위반 위험: Python `os.open`은 `FILE_SHARE_DELETE` 없이 파일을 연다. attacher가 endpoint를 읽는 순간 owner의 `os.replace`(게시)나 `os.unlink`(철회)가 `PermissionError`로 실패할 수 있다. 현재 재시도·분류가 없어 원시 `PermissionError`가 전파된다. 게시 실패 시 launch는 잠금을 해제하고 실패한다. 철회 실패 시 shutdown은 잠금을 해제하지만 예외를 전파한다. Windows에서 재현·검증하지 않았다.
- named pipe·unix socket 서버, 접근 제어(사용자별 ACL, `FILE_FLAG_FIRST_PIPE_INSTANCE`), 재연결 handshake. unix socket 경로 길이 제한(약 108바이트)도 서버 구현 시 확인해야 한다.
- Electron GUI 창 이벤트와의 실제 연결. attach한 두 번째 프로세스가 owner의 창 상태를 바꾸는 경로(IPC)는 없다. 사용자 세션 경계(다른 Windows 사용자·RDP 세션)에서의 격리도 검증하지 않았다.
- 잔여 경쟁 창:
  - 시작 시: 이전 owner가 죽은 직후 새 owner가 잠금을 얻고 stale endpoint를 지우기 전의 짧은 순간, attach 측이 이전 기록을 읽을 수 있다.
  - 종료 시: owner가 `shutdown()`에 들어가기 직전에 endpoint를 읽은 attach 측은 곧 사라질 인스턴스의 기록을 받을 수 있다. 종료 스캔 중의 attach는 endpoint 선철회로 막았지만, 이 직전 순간은 남아 있다.
  - 두 경우 모두 최종 확인은 endpoint handshake에서 instance_id를 대조해야 하며, 이 handshake는 아직 구현되지 않았다.

이 문서와 시험 결과는 AIK-006의 완료 판정이 아니다. 장부 상태는 변경하지 않는다.
