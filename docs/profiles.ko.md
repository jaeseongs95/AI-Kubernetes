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
- application ID: `0x41494B50`(`AIKP`)

기존 `AgentProfile`·`Workspace` 테이블은 쓰지 않는다. `SettingsStore`에서는 저장 시점의 참조 확인만 읽는다.

- workspace가 존재해야 한다.
- primary와 모든 fallback의 connection이 같은 workspace에 존재해야 한다.
- 없는 참조는 `InvalidReference`, 다른 workspace의 connection은 `CrossWorkspaceReference`로 거절한다.

기존 `AgentProfile`은 role이 director/worker뿐이고 tools·한도·fallback 필드가 없다. 그래서 변환 함수만 제공한다.

- `to_agent_profile(profile)`: 읽기 전용 view를 만든다. reviewer는 대응 role이 없어 `None`을 반환한다.
- `from_agent_profile(agent, max_calls=..., ...)`: 기존 설정에서 실행 프로필을 만든다.

**역할별 독립 revision**: 프로필 하나가 하나의 행이며 각자 revision을 가진다. 갱신과 삭제는 그 행의 `expected_revision`만 확인한다. 따라서 총괄 프로필을 바꾸어도 worker·검토자 행은 바뀌지 않는다. 갱신할 때 `workspace_id`나 role을 바꾸면 `InvalidProfile`로 거절한다. 이 규칙은 총괄이 다른 역할의 행을 덮어쓰는 경로도 막는다.

API는 `save(profile, expected_revision=0)`, `get(id)`, `list(workspace_id, role=None, limit, after_id)`, `delete(id, expected_revision)`이다. `0`은 새로 생성할 때만 쓴다.

## 실행 직전 capability 검증과 provenance

`prepare_execution(profile_id, inventory, approval=None, clock=None)`의 처리 순서는 다음과 같다.

1. 저장된 프로필을 읽고 settings 참조를 다시 확인한다. 참조가 사라졌으면 capability 검사 없이 `REFERENCE_MISSING`(또는 `CROSS_WORKSPACE_REFERENCE`)으로 거절한다.
2. primary `(connection, model, effort, tools)`를 `inventory.validate`로 검사한다. `provider_observed` 또는 `configured_catalog` 근거로 수락된 경우에만 binding한다.
3. primary가 거절되면 **fallback을 자동으로 고르지 않는다.** `FallbackApproval(profile_id, profile_revision, fallback_index, approval_ref)`가 해당 프로필 ID·현재 revision·존재하는 위치를 정확히 가리킬 때만 그 위치 하나를 검사한다. 다음 경우에는 거절한다.
   - 승인이 없을 때: `FALLBACK_NOT_APPROVED`
   - 다른 프로필의 승인: `FALLBACK_APPROVAL_PROFILE_MISMATCH`
   - 이전 revision에 대한 승인: `FALLBACK_APPROVAL_STALE_REVISION`
   - 범위를 벗어난 위치: `FALLBACK_INDEX_OUT_OF_RANGE`
   - 승인된 fallback도 미지원일 때: `APPROVED_FALLBACK_REJECTED`
4. inventory 예외는 `CAPABILITY_ERROR:<코드>`(예: `UNKNOWN_RESOURCE`)로 처리한다. 형식이 맞지 않는 결과도 거절로 처리한다(fail-closed).

반환값 `ExecutionDecision`에는 다음이 함께 들어간다.

- 요청 프로필 전체(`requested_profile`)와 그 revision
- 요청 primary
- 실제 binding(`bound`, `bound_position`)
- 사용된 승인
- 모든 시도의 `ValidationResult`
- 실제 binding의 provenance: `supported_by`, `evidence_digest`, connection·resource revision, `validated_at`

같은 내용은 `to_document()` JSON으로 `execution_records` 테이블에 append된다. `execution_records(profile_id)`로 조회할 수 있다. 거절된 결정도 기록된다. 실행 측은 반드시 `decision.require()`를 호출한다. 이 호출은 미binding이면 `CapabilityMismatch`를 발생시킨다. 시각은 주입한 `clock`을 사용한다.

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
- 총괄 변경이 worker/검토자를 덮어쓰지 않음, role/workspace 변경 거절
- 참조 검사
- primary binding과 provenance 기록
- effort/tool 불일치 거절과 자동 fallback 금지
- 명시 승인 fallback, 무효·이전·미지원 승인
- 미등록 resource와 근거 없음
- 삭제된 connection
- AgentProfile 어댑터
- 검증 명령의 값 비노출
- 외부 DB 보존

시험의 inventory는 실제 `CapabilityInventory`다. 다만 resource 등록은 시험 전용 harness가 내부 `_resources`에 합성 binding을 넣는 방식이다. 실제 `aik_provider.ConnectionBinding` 경로는 이 시험으로 검증하지 않는다.

## 한계·미검증(NOT_RUN)

- Windows 기본 경로와 Windows 잠금 동작은 **미검증(NOT_RUN)**이다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
- 실제 provider·실계정의 capability 조회와 실제 실행 연결은 미검증이며 후행 태스크 범위다. 이 패키지는 실행을 수행하거나 인가하지 않는다.
- settings 파일과 profiles 파일은 서로 다른 파일이다. 그래서 connection 삭제를 DB 수준에서 막지 못한다. 대신 실행 직전에 참조를 다시 확인한다.
- `FallbackApproval.approval_ref`는 형식만 검사한다. 승인 기록이 실제로 있는지, 누가 승인했는지는 검증하지 않는다.
- `max_calls`·budget은 저장값이며, 호출 집행·예산 차감은 이 패키지가 하지 않는다.
- `planning/` 장부의 상태와 완료 판정은 변경하지 않았다.
