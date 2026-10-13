# AIK-010 — 다중 계정·구독·API 연결 metadata

`src/aik_connection/`은 같은 벤더의 여러 계정과 구독/API 실행 경로를 별도 connection으로 등록·조회·이름 변경·재결속·삭제하는 API를 제공한다. 표준 라이브러리만 사용하며 실제 provider·공식 호스트를 호출하지 않는다. 기존 `aik_config`·`aik_vault`·`aik_runtime` 코드는 수정하지 않았다.

## 저장 위치와 구조

| 위치 | 내용 |
| --- | --- |
| `aik_config.SettingsStore`의 `ConnectionProfile` | 기존 최소 참조: `id`(=connection_id)·workspace·provider·`auth_mode`(=auth_kind)·`needs_authentication` |
| 같은 폴더의 `connections.sqlite3` (application ID `0x41494B4E`, `AIKN`) | account label, auth_kind, execution_kind, 공식 호스트(`official_host`), 호스트 프로필(`host_profile`), 비민감 계정 binding(`account_binding`), `binding_revision`, `credential_ref`, 확인 시각(`verified_at`), 불변 `provenance_id`, revision |

aik_config의 schema는 추가 테이블을 허용하지 않으므로 상세 metadata는 별도 파일(어댑터)에 둔다. 경로 검증·프로세스 잠금은 `aik_runtime.paths`를 재사용하고, 기존 파일은 header의 application ID를 확인한 뒤에만 연다(설정 DB·runtime DB·외부 파일 거절). WAL 동반 파일·고아 journal이 있으면 거절하며, 파일이 사라져도 몰래 재생성하지 않는다.

## 실행 경로 규칙

| execution_kind | auth_kind | credential_ref | account_binding |
| --- | --- | --- | --- |
| `api` | `api_key` | 필수: `aik_vault`가 발급한 `secret://aik-vault/<32 hex>`만 | 필수 |
| `subscription` | `host_session` | 없음(세션은 공식 호스트 소유) | 필수 |
| `local` | `none` | 없음 | 없음 |

- 시크릿 값은 받지도 저장하지도 않는다. 등록 시 `VaultCredentialRefChecker`(aik_vault 공개 `resolve`를 호출해 결과를 즉시 버림)로 참조 존재를 확인한다. 확인 어댑터가 없거나 vault가 응답하지 못하면 `CredentialRefUnavailable`로 거절한다(fail-closed).
- `account_binding`은 식별자 규칙(`[A-Za-z0-9][A-Za-z0-9._:-]*`)만 허용하므로 이메일 등은 넣을 수 없다. 비민감 계정 ID만 쓴다.

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
- `mark_verified(id, observed_context, expected_revision, verified_at=None)`: 공식 호스트 어댑터가 관측한 `AuthContext`가 등록 binding과 정확히 일치할 때만 확인 시각(UTC `Z`로 정규화)을 기록하고 설정의 `needs_authentication`을 해제한다. 관측 자체는 이 패키지가 하지 않는다.
- `rebind(id, expected_revision, official_host=…, host_profile=…, account_binding=…)`: `binding_revision`을 1 올리고 확인 시각을 지운다(재인증 필요).
- `verify_auth_context(context)`: 실행 직전 검증. connection_id·provider·auth/execution kind·공식 호스트·프로필·계정·binding revision 중 하나라도 다르면 `AuthContextMismatch`, 현재 binding revision이 미확인이면 `AuthenticationRequired`, API 연결의 vault 참조가 사라졌으면 `CredentialRefUnavailable`. 성공하면 label이 없는 `ConnectionProvenance`를 반환한다.
- `impact(id)` / `delete(id, expected_revision, force=False, acknowledged_impact=None)`.

## 중복 판정

다음은 `DuplicateConnection`이며 아무 레코드도 쓰지 않는다: 같은 connection_id(기존 aik_config 연결 포함), 같은 workspace의 같은 label(NFKC·casefold 비교), 같은 workspace·provider·execution_kind·공식 호스트·account_binding, 같은 vault credential_ref. 같은 계정의 구독과 API는 execution_kind가 다르므로 별도 등록된다.

## 이름 변경과 provenance

`connection_id`, `provenance_id`, workspace, provider, auth/execution kind, credential_ref는 갱신 API로 바꿀 수 없다. task provenance는 `ConnectionProvenance`(label 제외)를 기록해야 하며 이름 변경 전후 값이 같다. 계정·호스트 변경은 `binding_revision` 증가로 구분된다.

## 삭제와 영향 범위

`impact()`는 해당 연결을 가리키는 settings `AgentProfile`(제거 가능)과 등록된 외부 참조 원천(예: `RuntimePoolReferences`가 보고하는 aik_runtime `Pool`, 제거 불가)을 `DeletionImpact`로 반환한다. 참조 원천이 응답하지 못하면 `ReferenceCheckUnavailable`로 삭제를 차단한다.

- 사용 중이면 기본적으로 `ConnectionInUse`(`.impact` 포함)로 거부한다.
- `force=True`는 `acknowledged_impact`가 현재 영향 범위와 **정확히 같을 때만** 허용되며, settings의 AgentProfile만 같은 settings transaction에서 함께 제거한다. 외부 참조(runtime pool 등)는 이 패키지가 지우지 않으므로 강제 옵션으로도 삭제되지 않는다.

## 두 파일 사이의 일관성

잠금 순서는 항상 상세 DB → 설정 DB다. 등록·갱신은 설정을 먼저 commit하고 상세를 commit하며, 삭제는 상세를 먼저 commit한다. 그 사이 실패에서 남을 수 있는 상태는 "상세 없는 settings ConnectionProfile"뿐이고, 이것은 관리 연결이 아니므로 `verify_auth_context`가 실패한다(fail-closed). 이 경우 `InconsistentConnectionStore`를 발생시키며 자동 복구는 하지 않는다. 상세는 있는데 settings 참조가 없거나 다르면(예: aik_config API로 직접 삭제) 검증은 `InvalidDatabase`로 실패한다.

## 검증과 한계

```powershell
python -m unittest tests.test_connection -v
```

- 시험은 OS 임시 폴더의 합성 데이터와 test-only `FakeVaultBackend`만 사용한다. 다중 계정 등록·재열기 유지, 중복, vault 참조 확인, 인증 문맥 일치/불일치·재결속, 이름 변경과 provenance 불변, 참조 중 삭제의 영향 범위·강제 옵션·외부 참조 차단, 외부 파일 거절을 검사한다.
- Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다. Windows 기본 경로·잠금·실제 DPAPI vault와의 연동은 **미검증(NOT_RUN)**이다.
- 두 파일 사이 부분 게시(commit 실패 주입) 경로는 시험으로 재현하지 않았다(**NOT_RUN**).
- `VaultCredentialRefChecker`는 aik_vault에 메타데이터 전용 존재 확인 API가 없어 `resolve`를 사용하므로 평문이 잠시 메모리에 생긴다. 저장·로그·반환하지 않지만 Python 문자열은 지울 수 없다.
- 실제 공식 호스트에서 `AuthContext`를 관측하는 어댑터, GUI 연결, AIK-011 `ConnectionBinding`과의 연동은 범위 밖이다. `planning/` 장부 상태는 변경하지 않았다.
