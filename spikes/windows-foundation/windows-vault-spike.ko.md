# AIK-002 Windows DPAPI 연결 spike

이 구현은 Windows GUI → sidecar → 현재 사용자 DPAPI의 연결 가능성을 확인하는 **폐기 가능한 AIK-002 spike**다. 실제 벤더 API 키·로그인 토큰을 입력하지 않으며, AIK-009 제품 vault의 완료 근거로 사용하지 않는다.

## 호출과 결과

Python 3.12 표준 라이브러리만 사용한다. 공개 함수는 인자가 없는 `aik_spike.vault.roundtrip()` 하나다. 모델·벤더·credential reference·사용자 지정 경로·시크릿을 받는 인터페이스는 제공하지 않는다.

```python
from aik_spike.vault import roundtrip

result = roundtrip()
```

성공 결과에는 상태와 고정 코드만 들어간다. 평문, 암호문, 평문 hash, 사용자명, 저장 경로, 원시 예외는 반환하거나 출력하지 않는다.

```json
{"status":"PASS","backend":"windows-user-dpapi","code":"VAULT_ROUNDTRIP_OK"}
```

Windows가 아니면 native DLL을 로드하거나 파일을 만들기 전에 종료한다. 다른 운영체제를 위한 모의 vault나 평문 저장 대체 경로는 없다.

```json
{"status":"NOT_RUN","backend":"windows-user-dpapi","code":"WINDOWS_REQUIRED"}
```

실행 중 오류가 생기면 `status: FAIL`과 허용된 고정 오류 코드만 반환한다. 비교에 성공했어도 이번 실행의 암호문 파일이나 디렉터리를 제거하지 못하면 `VAULT_CLEANUP_FAILED`로 실패 처리한다.

## 실제 Windows에서 수행하는 동작

1. 현재 사용자의 `FOLDERID_LocalAppData` 위치를 `SHGetKnownFolderPath`로 받는다. `LOCALAPPDATA` 환경변수를 저장 경로의 근거로 사용하지 않는다. API의 사용자 토큰 인자는 `NULL`이다. Microsoft 문서에서 이는 현재 사용자의 알려진 폴더를 뜻한다. [SHGetKnownFolderPath](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath), [FOLDERID_LocalAppData](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid#folderid_localappdata)
2. `<LocalAppData>\AI-Kubernetes\spikes\windows-foundation\vault\run-<무작위 값>`에 이번 호출만의 디렉터리를 만든다. 앱 디렉터리의 기존 symlink/reparse point를 거부한다. 다른 실행 디렉터리나 다른 서비스의 credential 저장소는 열거하지 않는다.
3. 소스에 공개된 고정 synthetic payload를 `CryptProtectData`로 암호화한다. `CRYPTPROTECT_UI_FORBIDDEN`만 설정하고 `CRYPTPROTECT_LOCAL_MACHINE`은 사용하지 않는다. description, optional entropy, prompt 인자는 `NULL`이다. Windows는 사용자 로그온 자격에 연결된 보호를 제공하며 machine flag는 같은 컴퓨터의 다른 사용자도 복호화할 수 있는 범위를 만든다. [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
4. 암호문만 임시 파일에 기록하고 `flush`·`fsync` 후 같은 실행 디렉터리의 `probe.dpapi`로 `os.replace`한다. 읽기와 쓰기는 64 KiB로 제한된다. 평문 파일이나 pickle은 만들지 않는다.
5. 기록된 파일을 다시 읽고 `CryptUnprotectData`로 복호화한다. native 평문 버퍼를 `hmac.compare_digest`로 고정 기대값과 비교한다. Python의 불변 `bytes`로 평문 복사본을 만들어 반환하지 않는다. Microsoft 문서는 변조 시 특정 오류 코드만을 믿지 말고 추가 무결성 검사를 고려하라고 명시한다. 이 spike의 oracle은 고정 기대값과의 일치다. [CryptUnprotectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata)
6. native 결과 버퍼와 Python의 변경 가능한 synthetic 입력 버퍼를 지우고, 이번 실행의 파일과 디렉터리를 삭제한다. 기존의 다른 실행·자격 증명 파일을 지우는 재귀 삭제 기능은 없다.

## Native 경계와 메모리 처리

`DATA_BLOB.cbData`는 32-bit `DWORD`, `pbData`는 포인터다. Windows x64에서도 길이를 64-bit 정수로 선언하지 않는다. `BOOL`과 `HRESULT`는 `ctypes.c_int32`, `LocalFree`의 인자·반환은 포인터 폭의 `ctypes.c_void_p`로 명시했다. DLL은 System32 검색 옵션으로 로드한다. [DATA_BLOB 선언](https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/ns-wincrypt-crypt_integer_blob)

DPAPI가 할당한 버퍼는 zero-fill 후 `LocalFree`로 해제하고, 실패 반환도 확인한다. 알려진 폴더 API가 할당한 경로는 API 성공 여부와 관계없이 `CoTaskMemFree`로 해제한다. 두 allocator를 혼용하지 않는다. 복호화 description을 요청하지 않으므로 그 출력에 대한 별도 할당은 생기지 않는다. [LocalFree](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-localfree), [CoTaskMemFree](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-cotaskmemfree)

현재 Python spike의 버퍼 지우기는 `ctypes.memset`을 실제 호출하는 방식이다. Microsoft의 `SecureZeroMemory` 문서가 다루는 C/C++의 사용 후 지우기 제거 최적화와 구분되며, Python 런타임·swap·프로세스 dump 전체의 평문 소거를 보장하지 않는다. 공개 synthetic 값이므로 이 spike는 실제 비밀 메모리 보호 구현의 완료로 보지 않는다. [SecureZeroMemory](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/legacy/aa366877(v=vs.85))

## 보안 경계와 미완료 범위

사용자 DPAPI는 **같은 Windows 사용자로 실행되는 다른 프로세스와의 격리 경계가 아니다.** 로그인된 사용자의 권한으로 동작하는 악성 프로세스, 관리자 권한, 실행 중인 프로세스의 메모리 노출을 이 spike가 차단했다고 주장하지 않는다. 이것은 DPAPI의 사용자 로그온 자격 기준과 이 구현의 실행 위치에서 도출되는 경계다. 같은 사용자의 roaming profile에 관한 DPAPI 예외도 존재한다. [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)

저장소는 현재 사용자의 LocalAppData 상속 ACL을 사용한다. 앱 디렉터리의 reparse point 확인은 잘못된 기존 경로를 거부하는 검사이며, 검사 이후 경로 교체를 시도하는 같은 사용자 공격자에 대한 보장을 만들지 않는다. 프로세스가 강제 종료되면 암호문 임시 파일이 남을 수 있다. 파일 원자적 교체는 전체 제품의 전원 장애 복구나 장기 보관 프로토콜을 대신하지 않는다.

AIK-009에는 별도로 credential reference/CRUD, 다른 사용자 접근, 재시작 후 기존 데이터 해독, 손상·복구 정책, 접근 제어와 제품 threat model의 검증이 남아 있다. 이 spike는 이를 완료하지 않는다.

## 재현 명령

저장소 루트에서 Windows Python 3.12로 실행한다. 실제 자격 증명이나 관리자 권한은 필요하지 않다.

```powershell
py -3.12 spikes/windows-foundation/python/aik_spike/vault.py
py -3.12 -m unittest discover -s spikes/windows-foundation/tests -p test_vault.py -v
```

CLI exit code는 성공 `0`, 실행 실패 `1`, Windows 미지원 `2`다. 테스트의 `WindowsNativeVaultTests` 2개는 Windows에서 실제 DPAPI를 호출한다. 나머지 테스트의 native 경계 모형과 장애 주입은 단위시험이며 Windows 실행 증거로 세지 않는다.

## 2026-10-12 이 작업 환경의 실행 결과

| 구분 | 실행 결과 | 의미 |
|---|---|---|
| 단위·파일 장애 주입 시험 | 13 PASS | 비 Windows 거부, 오류 출력 차단, 메모리 wipe 호출, 타입 폭, 파일 교체 실패·정리 검증 |
| 실제 Windows DPAPI 시험 | 2 SKIP / NOT_RUN | 현재 작업 환경이 Linux이므로 Windows API를 호출하지 않음 |
| vault CLI 실제 실행 | exit 2, `WINDOWS_REQUIRED`, stderr 없음 | 미지원 상태를 실제 출력했으며 DPAPI 성공을 주장하지 않음 |

따라서 구현 코드는 존재하지만, **이 기록만으로 Windows DPAPI 왕복의 PASS를 선언하지 않는다.** Windows 실행 환경에서 위 native 시험과 GUI/sidecar 연결을 실행한 별도 근거가 필요하다.
