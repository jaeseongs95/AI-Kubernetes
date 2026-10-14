# OS port 계층 (`aik_platform`)과 벤더 중립 HTTP (`aik_http`)

상태: **구현(부분)**. 대응 작업은 AIK-091(OS port 계층)과 AIK-092(공통 코드 벤더 결합 제거)이고, 근거 결정은 DEC14, 설계 원칙은 PRN-OS·PRN-VENDOR·PRN-SOLID다. 이 문서는 원장 상태를 바꾸지 않는다.

## 원칙

- 공통 코드는 운영체제를 모른다. OS 차이는 `aik_platform`의 concern별 port 뒤에만 두고, `os.name`·`sys.platform` 판별은 이 패키지에서만 한다(`aik_platform/_os.py`).
- 새 OS는 이 패키지에 adapter를 더해 지원한다. 호출하는 쪽은 고치지 않는다(OCP). port는 concern별로 작게 나눈다(ISP).
- 공통 패키지는 벤더 adapter(`aik_adapter_*`)나 호스트 bridge(`aik_host_*`)를 import하지 않는다. 네트워크가 필요하면 벤더 중립 `aik_http`를 쓴다.
- 두 경계는 정적 시험으로 고정한다(`tests/test_platform.py`의 `PlatformBoundaryTests`, `VendorBoundaryTests`).
  - OS 판별 예외는 한 곳뿐이다: Windows DPAPI adapter(`aik_vault/dpapi.py`)의 `WinDLL` 사용.

## port 목록

| 모듈 | 공개 함수 | Windows | Linux(POSIX) | 이전 위치 |
|---|---|---|---|---|
| `fs` | `sync_file`, `sync_directory`, `restrict_to_owner`, `is_link_or_reparse_point`, `paths_case_insensitive` | 디렉터리 fsync 생략, 상속 ACL, reparse 속성, 대소문자 무시 | 디렉터리 fsync, 0600/0700, symlink | 13개 패키지에 복제된 `_sync_directory`(13벌)·`_sync_file`(6벌), reparse 검사 4벌 |
| `locks` | `lock_ops(descriptor)` | `msvcrt` byte 0 | `flock` | `aik_runtime.paths.database_lock`, `aik_supervisor._lock_ops` |
| `paths` | `state_root`, `component_dir`, `component_file`, `is_local_drive`, `supports_native_cwd` | `%LOCALAPPDATA%\AIK\<구성요소>\…` | `$XDG_STATE_HOME/aik/…`(기본 `~/.local/state/aik`) | runtime·설정·workspace·vault·supervisor의 경로 분기 |
| `process` | `popen_options`, `supervise`, `terminate_tree`, `kill_group`, `still_tracked`, `process_cmdline`, `no_window_flags`, `new_console_flags` | 프로세스 그룹, Job Object, `taskkill` | 새 세션, `killpg`, `/proc` | 바이트까지 같던 `aik_host_claude/_process.py`·`aik_host_codex/_process.py`, broker·devtools·ags_peer 사본 |
| `session` | `session_locked` | 입력 데스크톱 전환 가능 여부 | systemd-logind `LockedHint`(`loginctl`) | `aik_auth_lifecycle.windows_session_locked` |

**비밀 저장(vault)**
- `aik_vault.open_user_vault`가 플랫폼별 backend를 고른다: Windows는 DPAPI, Linux는 Secret Service. 자세한 내용은 [vault](vault.ko.md)에 있다.
- 파일 backend 공통 코드는 위 `fs`·`paths` port를 쓴다.

**HTTP**
- `aik_http.transport`는 원래 Anthropic adapter 안에 있던 범용 transport다(`UrllibTransport`, redirect 거절, 응답 상한, 헤더·본문 repr 제외).
- `aik_jev`는 이제 이 모듈을 쓴다. `aik_adapter_anthropic.transport`는 호환을 위한 재노출만 한다.
- OpenAI adapter의 transport는 계약과 동작이 달라 그대로 두었다. 둘을 합치는 일은 AIK-092의 남은 범위다.

**호환성**
- 기존 공개 이름과 동작은 그대로다. 각 모듈의 `_sync_directory` 같은 비공개 이름은 port 함수를 가리키는 별칭으로 남겼다.
- 파일 권한, 오류 코드, 잠금 의미, 경로 배치(Linux는 평평한 `aik/` 아래)는 바뀌지 않았다. 기존 전체 회귀로 확인했다.
- 바뀐 시험은 세 건이다(`test_scope`, `test_connection`, `test_devtools`). 이 시험들은 "다른 패키지의 비공개 헬퍼에 기대지 않는다"는 검토 결과(F9·F10)를 복제 코드로 고정하고 있었다. 이제는 같은 의도를 "공개 port를 쓴다"로 검사한다.
- 진단(`aik_compat.diagnostics`)은 provider 이름을 코드 목록으로 제한하던 것을 식별자 형식 검사로 바꿨다. 지원 벤더 목록은 지원 matrix 데이터(DEC12)에 있다.

## 검증

- 로컬(Linux): 전체 회귀가 통과했다. 실제 키링 시험 1건은 환경 변수가 없어 skip이다.
- CI `platform-linux.yml`: ubuntu-24.04에서 실제 GNOME Keyring(합성 암호)으로 Secret Service 왕복 시험을 돈다.
- 기존 Windows CI(aik-modules의 windows-2022): Windows adapter(DPAPI, `msvcrt`, Job Object 경로)를 다시 검증한다.
- **NOT_RUN**:
  - 실제 데스크톱 세션의 logind 잠금 신호
  - KWallet 등 다른 Secret Service 구현
  - 실제 Linux 데스크톱 GUI 실행(AIK-045의 LINUX-X64 행)
  - Linux에서 VM 실행에 의존하는 경로(외부 선행 EXT-VM-POSIX-D1)
