# AIK-023 — 총괄·worker·검토자 실행 프로필과 모델 지정

`src/aik_profiles/`는 총괄(`DirectorProfile`)과 worker/검토자(`RoleProfile`) 실행 프로필의 schema, 저장 어댑터, 실행 직전 capability 검증, 검증 명령을 제공한다. 표준 라이브러리만 사용한다. 다음 기존 패키지는 import해서 재사용하며 수정하지 않는다.

- `aik_config`: `SettingsStore`(Workspace·ConnectionProfile 참조 확인), `AgentProfile`, 식별자·정수 검증, `BUDGET_UNITS`, `Revisioned`
- `aik_capability`: `CapabilityInventory.validate`, `ValidationResult`, `Provenance`
- `aik_runtime.paths`: 경로 검증과 프로세스 잠금

provider 호출, credential 읽기, 실행 권한 부여는 하지 않는다.

## schema

| 모델 | 필드 |
| --- | --- |
| `ExecutionChoice` | `connection_id`(실행 자원), `model`, `effort`(선택), `tools`(tuple, 대소문자 무시 중복 금지, 최대 64개) |
| `DirectorProfile` | `id`, `workspace_id`, `primary`, `max_calls`(호출 한도), `fallbacks`(허용 대체 프로필의 순서 있는 tuple, 최대 8개), `budget_limit`, `budget_unit`. 역할은 항상 `director` |
| `RoleProfile` | `DirectorProfile`과 같은 필드에 `role`(`worker`/`reviewer`)을 추가 |

모든 모델은 frozen dataclass다. 비밀 값을 담는 필드는 없다. 추가 필드는 `TypeError`로 거절한다. 그 밖의 잘못된 값은 `InvalidProfile`로 거절한다.

- 0·음수·bool `max_calls`
- 알 수 없는 budget 단위
- primary와 같은 fallback, 서로 중복된 fallback
- list로 전달한 tools·fallbacks

`object.__setattr__`로 변조한 객체도 저장할 때 재구성·재검증하므로 거절된다. 오류 메시지에는 입력 값이 들어가지 않는다.

## 저장: SettingsStore와 충돌하지 않는 어댑터

`ProfileStore(path=None, *, settings=SettingsStore, timeout=5.0)`는 별도 SQLite 파일을 사용한다.

- 기본 위치: settings 파일 옆의 `profiles.sqlite3`. Windows에서는 `%LOCALAPPDATA%\AIK\config\profiles.sqlite3`이다.
- application ID: `0x41494B50`(`AIKP`), schema version **2**
- settings 파일 자체뿐 아니라 그 SQLite companion(`-journal`·`-wal`·`-shm`)과 `.storage.lock` 이름도 프로필 경로로 쓸 수 없다. 반대로 프로필 파일의 companion 이름이 settings 파일과 겹쳐도 거절한다(`InvalidProfile`, 파일 생성 전 판정).

기존 `AgentProfile`·`Workspace` 테이블은 쓰지 않는다. `SettingsStore`에서는 저장 시점의 참조 확인만 읽는다.

- workspace가 존재해야 한다.
- primary와 모든 fallback의 connection이 같은 workspace에 존재해야 한다.
- 없는 참조는 `InvalidReference`, 다른 workspace의 connection은 `CrossWorkspaceReference`로 거절한다.

기존 `AgentProfile`은 role이 director/worker뿐이고 tools·한도·fallback 필드가 없다. 그래서 변환 함수만 제공한다.

- `to_agent_profile(profile)`: 읽기 전용 view를 만든다. reviewer는 대응 role이 없어 `None`을 반환한다.
- `from_agent_profile(agent, max_calls=..., ...)`: 기존 설정에서 실행 프로필을 만든다.

**역할별 독립 revision**: 프로필 하나가 하나의 행이며 각자 revision을 가진다. 갱신과 삭제는 그 행의 `expected_revision`만 확인한다. 따라서 총괄 프로필을 바꾸어도 worker·검토자 행은 바뀌지 않고, worker·검토자를 바꾸어도 총괄 행은 바뀌지 않는다. 갱신할 때 `workspace_id`나 role을 바꾸면 `InvalidProfile`로 거절한다. 이 규칙은 총괄이 다른 역할의 행을 덮어쓰는 경로도 막는다.

API는 `save(profile, expected_revision=0)`, `get(id)`, `list(workspace_id, role=None, limit, after_id)`, `delete(id, expected_revision)`이다. `0`은 새로 생성할 때만 쓴다.

**삭제 후 재생성 revision**: 삭제하면 그 ID의 마지막 revision을 `profile_tombstones`에 남긴다. 같은 ID를 다시 생성하면 revision은 1이 아니라 **삭제된 revision + 1**부터 시작한다. 따라서 이전 프로필에 대해 발급된 `FallbackApproval`은 재생성된 프로필(다른 connection·model·workspace 포함)과 revision이 일치하지 않아 `FALLBACK_APPROVAL_STALE_REVISION`으로 거절된다. 처음 생성하는 ID는 여전히 revision 1이다.

**schema 업그레이드**: version 1 파일은 처음 열 때 무결성·레코드 검사를 거친 뒤 한 SQLite 트랜잭션에서 tombstone 테이블을 추가하고 version 2로 올린다. 실패하면 rollback되어 version 1 상태가 유지된다. version 1 시절에 이미 삭제된 ID의 revision은 기록이 없으므로 복원할 수 없다. 알 수 없는 version이나 이전 코드로의 downgrade는 지원하지 않으며 거절한다.

## 실행 직전 capability 검증과 provenance

`prepare_execution(profile_id, inventory, approval=None, clock=None)`의 처리 순서는 다음과 같다.

1. 저장된 프로필을 읽고 settings 참조를 다시 확인한다. 참조가 사라졌으면 capability 검사 없이 `REFERENCE_MISSING`, 다른 workspace로 옮겨졌으면 `CROSS_WORKSPACE_REFERENCE`로 거절한다.
2. primary `(connection, model, effort, tools)`를 `inventory.validate`로 검사한다. `provider_observed` 또는 `configured_catalog` 근거로 수락되고 요청 provenance가 `requested`인 경우에만 binding한다.
3. primary가 거절되면 **fallback을 자동으로 고르지 않는다.** `FallbackApproval(profile_id, profile_revision, fallback_index, approval_ref)`가 해당 프로필 ID·현재 revision·존재하는 위치를 정확히 가리킬 때만 그 위치 하나를 검사한다. 다음 경우에는 거절한다.
   - 승인이 없을 때: `FALLBACK_NOT_APPROVED`
   - 다른 프로필의 승인: `FALLBACK_APPROVAL_PROFILE_MISMATCH`
   - 이전 revision(삭제 전 프로필 포함)에 대한 승인: `FALLBACK_APPROVAL_STALE_REVISION`
   - 범위를 벗어난 위치: `FALLBACK_INDEX_OUT_OF_RANGE`
   - 승인된 fallback도 미지원일 때: `APPROVED_FALLBACK_REJECTED`
4. inventory 예외는 `CAPABILITY_ERROR:<코드>`(예: `UNKNOWN_RESOURCE`, 코드 형식이 아니면 `UNCLASSIFIED`), 그 밖의 예외는 `INVENTORY_UNAVAILABLE`로 처리한다. 요청과 다른 결과는 `VALIDATION_RESULT_MISMATCH`, 근거 없는 수락은 `NO_SUPPORT_EVIDENCE`로 거절한다(fail-closed). 예외 원문은 기록하지 않는다.

반환값 `ExecutionDecision`에는 다음이 함께 들어간다.

- 요청 프로필 전체(`requested_profile`)와 그 revision
- 요청 primary
- 실제 binding(`bound`, `bound_position`)
- 사용된 승인
- 모든 시도의 `ValidationResult`
- 실제 binding의 provenance: `supported_by`, `evidence_digest`, connection·resource revision, `validated_at`

같은 내용은 `to_document()` JSON으로 `execution_records` 테이블에 append된다. 거절된 결정도 기록된다. 시계가 실패하면 결정과 기록 모두 만들지 않는다(`ProfileError`). 실행 측은 반드시 `decision.require()`를 호출한다. 이 호출은 미binding이면 `CapabilityMismatch`를 발생시킨다. 시각은 주입한 `clock`을 사용한다.

기록 조회:

- `execution_records(profile_id=None, limit=100, *, after_seq=None, before_seq=None, newest_first=False)`: 결정 문서의 tuple. 기본은 오래된 순서다. 최신 결정은 `newest_first=True`(예: `limit=1`)로 읽는다.
- `execution_record_entries(...)`: 같은 인자로 `(seq, 문서)` 쌍을 반환한다. `seq`를 `after_seq`(오름차순 다음 페이지) 또는 `before_seq`(내림차순 다음 페이지) cursor로 써서 1000건을 넘는 기록도 순회한다.

## 검증 명령

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m aik_profiles validate director.json worker.json
```

입력 문서 형식은 다음과 같다.

```json
{"format": "aik.role-profile", "format_version": 1,
 "profile": {"id": "director-1", "workspace_id": "ws-1", "role": "director",
   "primary": {"connection_id": "conn-a", "model": "synthetic-model-alpha", "effort": "high", "tools": ["shell"]},
   "fallbacks": [], "max_calls": 40, "budget_limit": null, "budget_unit": "usd_cents"}}
```

- 정확한 키 집합만 허용한다. 중복 키와 NaN은 거절한다.
- 정규 파일만 읽는다. 디렉터리·FIFO 등은 열기 전에 `NotRegularFile`로 거절하고, 최대 1 MiB + 1바이트까지만 읽어 초과 시 `DocumentTooLarge`로 거절한다.
- 결과는 파일마다 JSON 한 줄이며, 실패 시 오류 분류명만 출력하고 값은 출력하지 않는다.
- 종료 코드: 모두 유효하면 0, 하나라도 무효면 1, 사용법 오류는 2

이 명령은 schema만 검증한다. capability 검증은 실행 직전 inventory와 함께 수행한다.

## 시험

```powershell
python -m unittest tests.test_profiles -v
```

모든 시험은 OS 임시 폴더와 합성 ID·합성 모델명만 사용하며, 시계를 주입한다. 검사 항목은 다음과 같다.

- 다중 벤더 프로필과 순서 있는 fallback의 저장·재열기
- 잘못된 값·옵션 조합, 변조 객체
- 총괄 변경이 worker/검토자를 덮어쓰지 않음, worker/검토자 변경이 총괄을 덮어쓰지 않음, 두 저장소 인스턴스 사이의 stale revision 충돌, role/workspace 변경 거절
- 삭제 후 재생성 시 revision 연속과 이전 fallback 승인 거절, version 1 파일의 업그레이드, 미지원 version 보존
- 계약을 위반한 저장 문서의 `InvalidDatabase` 분류
- 참조 검사(저장 시점·실행 시점의 누락과 workspace 이동)
- primary binding과 provenance 기록, `configured_catalog` 근거 binding
- model·effort·tool 불일치 거절과 자동 fallback 금지
- 명시 승인 fallback, 무효·이전·미지원 승인
- 미등록 resource, 근거 없음, inventory 예외·불일치 결과·`requested`가 아닌 결과
- 시계 실패 시 기록 없음, 최신 기록 조회와 seq cursor 순회
- AgentProfile 어댑터
- 검증 명령의 값 비노출, 크기 초과·디렉터리·FIFO 거절(FIFO 시험은 `os.mkfifo`가 있는 플랫폼에서만 실행)
- 외부 DB 보존, settings 파일·companion 이름 거절

시험의 inventory는 대부분 실제 `CapabilityInventory`다. 다만 resource 등록은 시험 전용 harness가 내부 `_resources`에 합성 binding을 넣는 방식이며, 일부 fail-closed 분기는 합성 결과를 반환하는 가짜 inventory로 검사한다. 실제 `aik_provider.ConnectionBinding` 경로는 이 시험으로 검증하지 않는다.

## 한계·미검증(NOT_RUN)

- Windows 기본 경로와 Windows 잠금 동작, Windows에서의 CLI 정규 파일 판정은 **미검증(NOT_RUN)**이다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
- 실제 provider·실계정의 capability 조회와 실제 실행 연결은 미검증이며 후행 태스크 범위다. 이 패키지는 실행을 수행하거나 인가하지 않는다. 실행 경로에서 `prepare_execution`/`require()`를 호출하는 통합 코드는 이 패키지에 없다.
- settings 파일과 profiles 파일은 서로 다른 파일이다. 그래서 connection 삭제를 DB 수준에서 막지 못한다. 대신 실행 직전에 참조를 다시 확인한다.
- `FallbackApproval.approval_ref`는 형식만 검사한다. 승인 기록이 실제로 있는지, 누가 승인했는지는 검증하지 않는다. 승인은 프로필 ID·revision·위치에 묶이며, 같은 revision 안에서 승인 대상 choice 자체를 별도로 대조하지는 않는다.
- `max_calls`·budget은 저장값이며, 호출 집행·예산 차감은 이 패키지가 하지 않는다.
- `planning/` 장부의 상태와 완료 판정은 변경하지 않았다.
