# AIK-009 — Windows 사용자 범위 시크릿 vault (ADR)

상태: **구현(부분) — 실제 Windows 검증 NOT_RUN**. 이 문서는 `src/aik_vault/`의 설계 결정 기록(ADR)이자 사용 안내다. 장부(`planning/`)의 AIK-009 상태와 완료 판정은 이 문서에서 바꾸지 않는다.

## 1. 맥락

API 키와 앱 소유 비밀은 runtime SQLite(`aik_runtime`)·설정·로그에 평문으로 남으면 안 된다. `aik_runtime.CredentialRef`는 이미 `secret://<namespace>/<path>` 형식의 **참조만** 저장하도록 설계돼 있다. 이번 태스크는 그 참조가 가리키는 실제 보관소를 Windows 로그인 사용자 범위로 제공한다.

## 2. 후보 비교와 선택

| 항목 | 사용자 범위 DPAPI (`CryptProtectData`) + 암호문 파일 | Windows Credential Manager (`CredWrite`/`CredRead`) |
|---|---|---|
| 저장소 내 실행 근거 | AIK-002 spike에서 GUI → Windows 사용자 DPAPI 왕복이 PASS로 기록됨(`docs/windows-packaging.ko.md`, run 38223818838, Windows Server 2022 관리자 runner). 이번 패키지 코드 자체의 실행 근거는 아님 | 자료에 없음 |
| 보호 범위 | 플래그 없이(또는 `CRYPTPROTECT_UI_FORBIDDEN`만) 호출하면 현재 사용자 자격에 결속. `CRYPTPROTECT_LOCAL_MACHINE`을 주면 머신 범위가 되므로 **사용 금지** | 사용자별 자격 증명 집합. 세부 정책 근거는 자료에 없음 |
| 동일 사용자 다른 앱과의 격리 | 제공하지 않음 — 저장소 문서가 이미 명시: "동일 사용자 권한의 다른 앱과의 격리까지 제공한다고 해석하지 않는다" | 자료에 없음(일반적으로 같은 사용자 프로세스는 읽을 수 있다고 알려져 있으나 본 저장소에서 미검증) |
| 크기·형식 제약 | 암호문을 앱 파일로 관리하므로 앱이 제한(현재 평문 16 KiB) | blob 크기 제한·roaming(persist) 정책 등은 자료에 없음(외부 문서 확인 필요, 미검증) |
| 백업·복구·제거 | 앱 데이터 폴더의 파일. 제거/백업 정책과 함께 다룰 수 있음 | OS 자격 증명 저장소 소관. 자료에 없음 |
| 구현 의존성 | `ctypes` + crypt32/kernel32. 표준 라이브러리만 사용 | `ctypes` + advapi32. 표준 라이브러리로 가능하나 이번 범위에서 구현하지 않음 |

**결정: 사용자 범위 DPAPI + 사용자 데이터 경로의 암호문 파일.** 선택 근거는 (1) 저장소에 이미 같은 계열의 Windows 실행 근거(spike)가 있고, (2) 표준 라이브러리 `ctypes`만으로 구현 가능하며, (3) 암호문 파일을 runtime 저장소와 같은 경로 규칙·잠금·symlink/hardlink 거절로 관리할 수 있기 때문이다. Credential Manager에 대한 정책 근거는 **자료에 없음**이므로 우열을 실측으로 주장하지 않는다. Credential Manager를 배제한 것이 아니라 이번 첫 구현 후보에서 제외한 것이며, 실기 비교는 NOT_RUN이다.

**머신 공유 범위는 기본값으로도, 옵션으로도 사용하지 않는다.** `PROTECT_FLAGS = CRYPTPROTECT_UI_FORBIDDEN(0x1)`이며, ctypes binding은 `CRYPTPROTECT_LOCAL_MACHINE(0x4)`이 들어간 호출을 `ValueError`로 거절한다. mock 시험은 모든 protect/unprotect 호출의 플래그에 0x4가 없음을 확인한다.

## 3. 보호 범위(경계)

| 상대 | 기대 결과 | 근거·상태 |
|---|---|---|
| 디스크·백업 사본을 가진 다른 OS 사용자 | 파일을 복사해도 해당 사용자 자격 없이는 복호화 실패 → `VAULT_ACCESS_DENIED` | DPAPI 사용자 범위 설계. 실제 다른 Windows 사용자 시험 **NOT_RUN** |
| 다른 OS 사용자의 파일 접근 | `%LOCALAPPDATA%` 상속 ACL에 의존(기본적으로 소유 사용자·SYSTEM·Administrators) | 앱이 ACL을 별도 설정하지 않음. **NOT_RUN** |
| **동일 사용자로 실행되는 native 프로세스** | **직접 접근 가능.** 같은 사용자는 암호문 파일을 읽고 `CryptUnprotectData`를 호출할 수 있다. entropy(`AIK-vault/v1/<ref id>`)는 파일 이름에서 유도되며 비밀이 아니다 | 경계 밖. 동일 사용자 악성코드·디버거 주입을 막지 않는다 |
| 관리자·SYSTEM·도메인 관리자 | 사용자 프로세스 주입, 도메인 DPAPI 백업 키 등으로 접근할 수 있는 경로가 있을 수 있음 | 경계 밖. 도메인 백업 키 관련은 일반 지식이며 자료에 없음·미검증 |
| 다른 PC로 파일만 이동 | 사용자 master key 없이 복호화 실패가 기대됨 | **NOT_RUN** |

entropy는 blob을 해당 참조에 결속하는 용도다. 다른 참조의 암호문 파일을 덮어 써도 복호화가 실패한다(fake·mock 시험으로만 확인).

## 4. API

```python
from aik_vault import open_user_vault, VaultError

vault = open_user_vault()            # Windows에서만 동작, 그 외 VaultUnavailable
ref = vault.put(api_key)             # 'secret://aik-vault/<32 hex>' 반환
vault.replace(ref, rotated_key)      # 같은 참조로 교체
plaintext = vault.resolve(ref)       # 호출 경계에서만 평문, 저장·로그 금지
vault.delete(ref)
```

- 백엔드 인터페이스 `VaultBackend`: `put/replace/delete/resolve`. 구현은 `DpapiVaultBackend`(제품)와 `FakeVaultBackend`(시험 전용).
- 참조는 `secrets.token_hex(16)`의 무작위 ID이며 비밀에서 유도하지 않는다. `aik_runtime.CredentialRef(id, ref)`의 locator 규칙을 만족하므로 SQLite에는 이 참조만 저장한다. 설정 파일에도 참조만 쓴다.
- `open_user_vault`는 Windows 외에서 fake로 **대체하지 않는다**(fail-closed). `FakeVaultBackend`는 명시적 디렉터리와 32-byte 키가 필수이며 보안 경계가 아니다.
- 평문은 `str`로 받아 내부 `bytearray`로 처리하고 사용 후 0으로 덮는다. Python의 불변 `str`/`bytes` 사본은 지울 수 없으므로 메모리 잔존 방지는 **best effort**다. DPAPI 출력 버퍼는 `LocalFree` 전에 0으로 덮는다.

## 5. 파일 위치·형식·동시성

- Windows 기본 위치: `%LOCALAPPDATA%\AIK\vault\`. `aik_runtime.paths.default_database_path()`의 규칙(LOCALAPPDATA 필수)을 재사용한다. 비 Windows의 `$XDG_STATE_HOME/aik/vault`는 개발·시험용 fallback이며 Linux 제품 지원이 아니다.
- 디렉터리와 각 암호문 파일은 `aik_runtime.paths.validate_database_path`로 검사한다: 절대 경로, `.git`/저장소 내부·개발 원장 이름 거절, symlink·reparse point·hardlink 거절, Windows 원격 드라이브 거절.
- 파일: `<ref id>.aikv` = `AIKV` + 형식 버전(1) + backend 태그(DPAPI `0x01`, fake `0xFE`) + 보호된 blob. 임시 파일 → fsync → `os.replace` → (POSIX) 디렉터리 fsync. POSIX에서 디렉터리 0700, 파일 0600.
- 프로세스 간 직렬화는 `aik_runtime.paths.database_lock`(`credentials.storage.lock`)을 재사용한다. 기한 초과는 `VAULT_BUSY`이며 쓰기를 수행하지 않는다.

## 6. 오류 반환

모든 오류는 고정 문구와 `code`만 가진다. 비밀·암호문·Windows 원시 예외를 포함하지 않으며, 인코딩 오류 예외(`UnicodeEncodeError.object`에 원문이 남는 문제)는 `__context__`로도 연결하지 않는다.

| code | 의미 |
|---|---|
| `INVALID_SECRET` | 빈 값·비 문자열·잘못된 유니코드·16 KiB 초과 |
| `INVALID_CREDENTIAL_REF` | `secret://aik-vault/<32 소문자 hex>`가 아님(경로 이동 포함) |
| `CREDENTIAL_NOT_FOUND` | 해당 참조의 암호문 파일 없음 |
| `VAULT_ACCESS_DENIED` | 파일 권한 거부 또는 현재 사용자가 복호화할 수 없음. **DPAPI는 다른 사용자 blob과 손상된 blob을 구분하지 않으므로 둘 다 이 코드다** |
| `VAULT_CORRUPT` | 헤더·형식·크기 불일치, 다른 backend가 쓴 파일, 복호화 결과가 UTF-8이 아님 |
| `VAULT_UNAVAILABLE` | Windows가 아님, DPAPI 로드/보호 실패 |
| `VAULT_BUSY` | 잠금 기한 초과 |
| `VAULT_IO_ERROR` | 그 밖의 파일 I/O 실패 |
| `VAULT_UNSAFE_PATH` | 경로 규칙 위반 |

## 7. 평문 비검출

`tests/test_vault.py`는 합성 비밀 두 개를 저장·교체한 뒤 runtime SQLite(참조 저장), 설정 JSON, 로그(경고·traceback 포함), `aik_security.OutputRedactor.output("crash_bundle", ...)` 결과와 이들을 묶은 무압축 ZIP 지원 번들, 암호문·잠금 파일을 전수 검사한다.

- 바이너리: UTF-8·UTF-16LE·hex(대/소문자)·base64(표준/URL-safe, 패딩 유무) 바이트열 비검출.
- 텍스트: 비밀을 등록한 redactor 결과가 빈 레지스트리 redactor 결과와 같아야 한다(= 등록 비밀 표현이 탐지되지 않음).
- 모든 vault 예외의 `str`·`repr`·traceback에 비밀이 없어야 한다.

이 검사는 시험이 만든 산출물의 범위이며, 제품 전체 출력 경로의 유출 방지 보증은 아니다(AIK-017 문서의 한계와 같다).

## 8. 검증 상태

```powershell
python -m unittest discover -s tests -p 'test_vault.py' -v
```

| 항목 | 상태 |
|---|---|
| fake 백엔드의 저장·교체·삭제, 실제 프로세스 재시작 후 참조 회수, 다른 키("다른 사용자" 모사), 손상, 잠금, 경로 거절, 평문 비검출 | 개발용 단위 시험(합성 데이터). 이 문서 작성 시점에 실행 결과 기록 없음 |
| DPAPI 백엔드의 플래그(LOCAL_MACHINE 미사용)·entropy·오류 매핑 | mock binding 단위 시험만 |
| 실제 Windows DPAPI 왕복(`test_real_dpapi_round_trip_on_windows`) | **NOT_RUN** (Windows 전용, Linux에서 skip) |
| 실제 다른 Windows 사용자·다른 PC·앱 재시작 후 DPAPI 회수 | **NOT_RUN** |
| `%LOCALAPPDATA%` ACL, 원격 드라이브 거절의 Windows 동작 | **NOT_RUN** |
| Credential Manager 실기 비교 | **NOT_RUN**, 정책 근거 자료에 없음 |

Linux 시험 통과는 개발 검증이며 Windows 지원 증거가 아니다. 완료 기준 1~3의 Windows 실측 부분이 남아 있으므로 이 구현만으로 AIK-009 완료를 주장하지 않는다.

## 9. 한계

- 동일 사용자 native 프로세스, 관리자·SYSTEM, 메모리 덤프는 보호 범위 밖이다.
- vault 파일 삭제는 ciphertext unlink이며 디스크 섹터의 안전 삭제가 아니다(내용은 DPAPI 암호문).
- 앱 제거 시 vault 처리, 백업·복구 정책, broker 프로세스 분리, 제품 GUI 연결은 후속 태스크 범위다.
