# AIK-010 — 다중 계정·구독·API 연결 metadata

`src/aik_connection/`은 같은 벤더의 여러 계정과 구독/API 실행 경로를 별도 connection으로 등록·조회·이름 변경·재결속·삭제하는 API를 제공한다. 표준 라이브러리만 사용하며 실제 provider·공식 호스트를 호출하지 않는다. 기존 `aik_config`·`aik_vault`·`aik_runtime` 코드는 수정하지 않았다.

## 저장 위치와 구조

| 위치 | 내용 |
| --- | --- |
| `aik_config.SettingsStore`의 `ConnectionProfile` | 기존 최소 참조: `id`(=connection_id)·workspace·provider·`auth_mode`(=auth_kind)·`needs_authentication` |
| 같은 폴더의 `connections.sqlite3` (application ID `0x41494B4E`, `AIKN`) | account label, auth_kind, execution_kind, 공식 호스트(`official_host`), 호스트 프로필(`host_profile`), 비민감 계정 binding(`account_binding`), `binding_revision`, `credential_ref`, 확인 시각(`verified_at`), 불변 `provenance_id`, revision |

aik_config의 schema는 추가 테이블을 허용하지 않으므로 상세 metadata는 별도 파일(어댑터)에 둔다.

- 경로 검증·프로세스 잠금은 `aik_runtime.paths`의 공개 함수를 재사용한다. 파일 동기화 함수는 패키지 안에 따로 두고, aik_runtime의 비공개 함수에 의존하지 않는다.
- 기존 파일은 header의 application ID를 확인한 뒤에만 연다. 따라서 설정 DB·runtime DB·외부 파일은 거절된다.
- 연결 파일 경로가 설정 DB와 같거나, 같은 폴더에서 설정 DB의 동반 파일 이름(`-journal`, `-wal`, `-shm`, `.storage.lock`, `.maintenance.lock`)과 겹치면 `UnsafeStoragePath`로 거절한다. 반대 방향으로 겹쳐도 거절한다.
- WAL 동반 파일이나 고아 journal이 있으면 거절한다.
- 이미 생성된 `ConnectionStore` 인스턴스는 사용 중에 파일이 사라져도 다시 만들지 않는다.
- **새** `ConnectionStore`를 생성할 때 연결 파일이 없으면 빈 파일을 새로 만든다. 이때 settings에 남아 있던 기존 `ConnectionProfile`은 상세 없는 참조가 된다. 이 참조는 실행 불가로 남지만(fail-closed), 고아 상태를 따로 감지·보고하지는 않는다.

## 실행 경로 규칙

| execution_kind | auth_kind | credential_ref | account_binding |
| --- | --- | --- | --- |
| `api` | `api_key` | 필수: `aik_vault`가 발급한 `secret://aik-vault/<32 hex>`만 | 필수 |
| `subscription` | `host_session` | 없음(세션은 공식 호스트 소유) | 필수 |
| `local` | `none` | 없음 | 없음 |

- 시크릿 값은 받지도 저장하지도 않는다.
  - 등록 시 `VaultCredentialRefChecker`로 참조가 존재하는지 확인한다. 이 어댑터는 aik_vault 공개 `resolve`를 호출하고 결과를 즉시 버린다.
  - 확인 어댑터가 없거나 vault가 응답하지 못하면 `CredentialRefUnavailable`로 거절한다(fail-closed).
- `account_binding`은 식별자 규칙(`[A-Za-z0-9][A-Za-z0-9._:-]*`)만 허용한다. 이메일 등은 넣을 수 없으며, 비민감 계정 ID만 쓴다.

## 사용법

```python
from aik_config import SettingsStore
from aik_vault import open_user_vault
from aik_connection import (ConnectionStore, ConnectionSpec, AuthContext,
                            VaultCredentialRefChecker)

settings = SettingsStore()
store = ConnectionStore(settings, credential_checker=VaultCredentialRefChecker(open_user_vault()))
store.register(ConnectionSpec('conn-sub-a', 'ws-1', 'vendor', '개인 구독', 'host_session',
                              'subscription', 'vendor-cli', 'default', account_binding='account-a'))
store.register(ConnectionSpec('conn-api-a', 'ws-1', 'vendor', 'API 키', 'api_key', 'api',
                              'vendor-api', 'default', account_binding='account-a',
                              credential_ref=ref))  # ref는 vault.put()이 반환한 opaque ID
```

- `register(spec)` → revision 1, `binding_revision` 1, 미확인 상태. 저장소가 `provenance_id`를 무작위로 발급한다.
- `get(id)`, `list(workspace_id=None)`, `provenance(id)`.
- `rename(id, label, expected_revision)`: label만 바꾼다.
- `mark_verified(id, observed_context, expected_revision, verified_at=None)`:
  - 공식 호스트 어댑터가 관측한 `AuthContext`가 등록 binding과 정확히 일치할 때만 동작한다.
  - 확인 시각(UTC `Z`로 정규화)을 기록하고 설정의 `needs_authentication`을 해제한다.
  - 관측 자체는 이 패키지가 하지 않는다.
- `rebind(id, expected_revision, official_host=…, host_profile=…, account_binding=…)`: `binding_revision`을 1 올리고 확인 시각을 지운다. 이후 재인증이 필요하다.
- `verify_auth_context(context)`: 실행 직전 검증이다.
  - connection_id·provider·auth/execution kind·공식 호스트·프로필·계정·binding revision 중 하나라도 다르면 `AuthContextMismatch`.
  - 현재 binding revision이 미확인이면 `AuthenticationRequired`.
  - API 연결의 vault 참조가 사라졌으면 `CredentialRefUnavailable`.
  - settings 참조가 상세와 맞지 않으면 실패한다(아래 "두 파일 사이의 일관성").
  - 성공하면 label이 없는 `ConnectionProvenance`를 반환한다.
- `impact(id)` / `delete(id, expected_revision, force=False, acknowledged_impact=None)`.

## 중복 판정

다음 경우는 `DuplicateConnection`이며, 아무 레코드도 쓰지 않는다.

- 같은 connection_id(기존 aik_config 연결 포함)
- 같은 workspace의 같은 label(NFKC·casefold 비교)
- 같은 workspace·provider·execution_kind·공식 호스트·account_binding
- 같은 vault credential_ref

`rebind`도 같은 규칙으로 검사한다. 같은 계정의 구독과 API는 execution_kind가 다르므로 별도로 등록된다. 다른 workspace에서는 같은 label·계정을 쓸 수 있다. 상세 파일의 제약 위반(SQLite `IntegrityError`)도 `DuplicateConnection`으로 보고한다.

## 이름 변경과 provenance

`connection_id`, `provenance_id`, workspace, provider, auth/execution kind, credential_ref는 갱신 API로 바꿀 수 없다. task provenance는 label이 제외된 `ConnectionProvenance`를 기록해야 하며, 이 값은 이름 변경 전후에 같다. 계정·호스트 변경은 `binding_revision` 증가로 구분된다.

## 삭제와 영향 범위

`impact()`는 해당 연결을 가리키는 참조를 `DeletionImpact`로 반환한다.

- settings의 `AgentProfile`: 제거 가능.
- 등록된 외부 참조 원천이 보고하는 레코드(예: `RuntimePoolReferences`가 보고하는 aik_runtime `Pool`): 제거 불가.
- 참조 원천이 응답하지 못하거나 지원하지 않는 레코드를 돌려주면 `ReferenceCheckUnavailable`로 삭제를 차단한다.
- `DeletionImpact.external_references_checked`는 외부 참조 원천이 하나라도 구성되어 조회되었는지를 나타낸다.
  - **원천을 주입하지 않은 기본 `ConnectionStore(settings)`는 runtime 등 외부 참조를 검사하지 않는다.** 이때 값은 `False`이며, 외부 사용 여부는 "알 수 없음"으로 해석해야 한다.
  - 실제 운영에서는 `reference_sources=(RuntimePoolReferences(runtime_store),)`처럼 원천을 주입한다.

삭제 규칙은 다음과 같다.

- 사용 중이면 기본적으로 `ConnectionInUse`(`.impact` 포함)로 거부한다.
- `force=True`는 `acknowledged_impact`가 현재 영향 범위와 **정확히 같을 때만** 허용한다. 이때 settings의 AgentProfile만 같은 settings transaction에서 함께 제거한다.
- 외부 참조(runtime pool 등)는 이 패키지가 지우지 않으므로 강제 옵션으로도 삭제되지 않는다.

한계는 다음과 같다.

- `acknowledged_impact`는 참조의 종류·ID·workspace·제거 가능 여부와 외부 검사 여부를 비교한다. AgentProfile의 내용이나 settings revision은 비교하지 않는다. 확인한 뒤 다른 프로세스가 같은 ID의 profile 내용을 바꿔도, 그 ID는 강제 삭제 대상에 포함된다.
- 외부 참조 조회와 삭제는 저장소 사이에서 원자적이지 않다. 외부 원천(runtime) 잠금은 조회 직후 해제된다. 따라서 조회와 삭제 사이에 다른 프로세스가 같은 connection_id를 쓰는 runtime Pool을 저장할 수 있다.
  - 이 경우 삭제는 성공하고, Pool은 존재하지 않는 connection_id를 가리킨다.
  - 이후 같은 ID를 다른 계정으로 재등록하면 그 Pool이 새 연결을 가리키게 된다.
  - 삭제된 connection_id의 재사용을 막는 tombstone은 구현하지 않았다. 운영에서는 삭제한 ID를 재사용하지 않는다.

## 두 파일 사이의 일관성

잠금 순서는 항상 상세 DB → 설정 DB다. commit 순서는 남을 수 있는 부분 상태가 모두 실행 불가가 되도록 작업별로 다르다.

| 작업 | commit 순서 | 두 commit 사이에서 실패하면 남는 상태 |
| --- | --- | --- |
| `register` | 설정 → 상세 | 상세 없는 settings `ConnectionProfile` |
| `rebind` 등 `needs_authentication`을 유지하거나 설정하는 갱신 | 설정 → 상세 | settings는 `needs_authentication=True`, 상세는 옛 binding·확인 시각 |
| `mark_verified` (`needs_authentication` 해제) | 상세 → 설정 | 상세는 확인됨, settings는 `needs_authentication=True`(설정만 읽는 소비자에게도 미인증으로 보임) |
| `delete` | 상세 → 설정 | 상세 없는 settings `ConnectionProfile` |

- 두 번째 commit이 실패하면 `InconsistentConnectionStore`를 발생시킨다. 첫 번째 commit 전에 실패하면 두 파일 모두 변경되지 않는다.
- `verify_auth_context`는 다음 순서로 판정한다.
  - 상세가 없으면 `UnknownConnection`.
  - settings 참조가 없거나 workspace·provider·auth_mode가 다르면(예: aik_config API로 직접 삭제·수정) `InvalidDatabase`.
  - settings의 `ConnectionProfile`이 상세에서 도출한 값(`needs_authentication` 포함)과 다르면 `InconsistentConnectionStore`.
  - 따라서 위 표의 어떤 부분 상태도 실행을 허용하지 않는다. 철회한 옛 계정 binding도 통과하지 못한다.
- 자동 복구는 하지 않는다. 의도한 `rebind` 또는 `mark_verified`를 현재 revision으로 다시 수행하면 settings가 상세와 맞춰진다.

## 검증과 한계

```powershell
python -m unittest tests.test_connection -v
```

시험은 OS 임시 폴더의 합성 데이터와 test-only `FakeVaultBackend`만 사용한다. 다음 항목을 검사하도록 작성했다.

- 다중 계정 등록·재열기 후 유지
- 중복(다른 workspace 허용, rebind 중복, INSERT 제약 위반 포함)
- 두 인스턴스의 동시 등록
- vault 참조 확인
- 인증 문맥 일치/불일치·재결속
- settings 참조 누락·불일치
- 손상 행 decode
- 이름 변경과 provenance 불변
- 참조 중 삭제의 영향 범위·강제 옵션·외부 참조 차단·참조 원천 장애
- 외부/runtime/settings 파일과 settings 동반 파일 이름 거절
- 부분 게시

부분 게시 시험의 실패 주입 방식은 다음과 같다.

- 상세 commit 실패: `sqlite3.Connection` 하위 클래스의 `commit` 예외로 모의한다.
- 설정 commit 실패: settings transaction을 감싼 래퍼의 예외로 모의한다.
- 실제 디스크 full·I/O 오류·전원 차단은 재현하지 않았다(**NOT_RUN**).

그 밖의 한계는 다음과 같다.

- Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다. Windows 기본 경로·잠금·실제 DPAPI vault와의 연동은 **미검증(NOT_RUN)**이다.
- `VaultCredentialRefChecker`는 `resolve`를 사용한다. aik_vault에 메타데이터 전용 존재 확인 API가 없기 때문이다. 그 결과 평문이 잠시 메모리에 생긴다. 저장·로그·반환하지 않지만 Python 문자열은 지울 수 없다.
- 실제 공식 호스트에서 `AuthContext`를 관측하는 어댑터, GUI 연결, AIK-011 `ConnectionBinding`과의 연동은 범위 밖이다.
- `planning/` 장부 상태는 변경하지 않았다.
