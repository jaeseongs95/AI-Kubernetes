# AIK-032 — AGS gate 결과 투영 (부분 구현)

`src/aik_vm_bridge/governance.py`는 VM이 이미 내린 governance gate(before_execution / before_completion) 결과를 공개 경로에서 읽는다. 읽은 결과는 표시용 상태(projection)로만 변환한다. AGS와 VM은 고정 계약이다. 이 모듈은 AGS 패키지나 VM 내부(SQL `history_events` 포함)를 import하거나 읽지 않는다. 상태를 저장하지 않으며 순수 함수와 frozen dataclass만 사용한다.

## 읽는 경로

VM 공개 `EngineApplication`에서 gate 결과를 읽는 경로는 두 가지다.

1. `run_once()` 결과의 `blocker_code`와 `detail`이다. `VmBridge.run_once_hosted`가 반환한 `BridgeReceipt.payload`에서 읽으며, `project_run_once(receipt)`가 처리한다.
2. `final_report().governance_plugin_tasks`다. `VmBridge.final_report`가 반환한 `BridgeReceipt.payload`에서 읽으며, `project_final_report(receipt)`가 처리한다. 이 값은 GoalVerdict가 있을 때만 VM이 제공한다.

보조 경로도 하나 있다. `project_status_blocked_tasks(receipt)`는 `status()`의 `vm_tasks`에서 `status == "blocked"`인 Task ID를 원문 순서대로 반환한다. 공개 표면에 사유가 없으므로 사유는 붙이지 않는다.

## 상태 매핑 (오직 `blocker_code`로 결정)

| VM `blocker_code` | `GateState` | `GatePhase` |
|---|---|---|
| `GOVERNANCE_GATE_PENDING` | `pending` | `unreported` |
| `GOVERNANCE_GATE_BLOCKED` | `blocked` | `before_execution` |
| `GOVERNANCE_GATE_FAILED` | `failed` | `before_completion` |
| `GOVERNANCE_CONTRACT_MISMATCH` | `contract_mismatch` | `unreported` (두 phase 모두에서 발생, phase는 비공개 History에만 있음) |
| 위에 없는 `GOVERNANCE_*` | `unrecognized` | `unreported` (fail-closed, 통과로 보지 않음) |
| 그 밖의 코드 | 투영하지 않음(`None`) | — |

나머지 입력은 다음과 같이 처리한다.

- `action != "blocked"`이면 `None`을 반환한다.
- `action == "blocked"`인데 `blocker_code`가 없거나 `^[A-Z][A-Z0-9_]{1,99}$`와 맞지 않으면 `VmBridgeError(INVALID_RESPONSE, subject="blocker_code")`를 발생시킨다.
- receipt의 command가 기대와 다르면 `INVALID_REQUEST`를 발생시킨다.

`EXTERNAL_EFFECT_UNKNOWN`은 gate 밖 경로에서도 같은 코드로 나온다. 따라서 governance 결과로 주장하지 않고 `None`을 반환한다. 이 코드가 gate에서 비롯됐는지는 공개 표면으로 구분할 수 없다. 이 부분은 한계로 남는다. 외부 효과 불명 처리 자체는 기존 bridge의 UNKNOWN guard가 담당한다.

## `detail`은 비권위 진단 텍스트

`GovernanceGateView.detail`은 VM 원문을 그대로 보존한 표시용 텍스트다. 파싱 결과는 `reason_code`·`check_id`·`conformance_check_id` 진단 필드만 채운다. **state와 phase를 바꾸지 않는다.** 예를 들어 `GOVERNANCE_GATE_BLOCKED`의 detail이 `GOVERNANCE_CONTRACT_MISMATCH: …`이어도 상태는 `blocked`로 유지되고, `check_id`만 채워진다.

파싱 규칙은 다음과 같다.

- 선택적인 `governance gate: ` 접두를 제거한다.
- 맨 앞 토큰이 `GOVERNANCE_[A-Z0-9_]{1,90}`이고 그 뒤가 문자열 끝·`:`·공백이면 사유 코드 후보로 본다. 알려진 사유 코드 목록(`KNOWN_REASON_CODES`) 밖의 토큰이면 `reason_code=None`으로 두고 detail은 유지한다.
- `GOVERNANCE_CONTRACT_MISMATCH: <검사 ID>: …` 형식이면 `check_id`를 채운다. 검사 ID가 `conformance:`로 시작하면 나머지 부분을 `conformance_check_id`로 둔다.
- 형식이 맞지 않거나 detail이 문자열이 아니면 해당 필드는 `None`이며 예외를 발생시키지 않는다.

알려진 사유 코드는 `GOVERNANCE_GATE_REQUIRED`, `GOVERNANCE_ROLE_CONFIGURATION_REQUIRED`, `GOVERNANCE_A2_PROFILE_REQUIRED`, `GOVERNANCE_PROVIDER_UNSUPPORTED`, `GOVERNANCE_USER_CHANGE_OVERLAP`, `GOVERNANCE_CONTRACT_MISMATCH`, `GOVERNANCE_TIMEOUT`, `GOVERNANCE_GATE_ERROR`다.

## plugin identity 관측

`PluginTaskObservation`은 VM 항목의 순서를 보존한다. 필드의 출처는 다음과 같다.

- `closure_tree_digest`는 VM이 manifest closure 파일로 계산한 값이며 출처는 `closure_provenance="local_derived"`다.
- `plugin_version_label`은 manifest의 자기 보고 label이며 출처는 `label_provenance="plugin_manifest_file"`이다. provider 관측이 아니므로 동등성·판정에 사용하지 않는다.
- `changed`와 `changed_phases`는 실행 중 plugin bytes identity가 바뀐 phase를 VM이 보고한 값이다.

payload 검증 규칙은 다음과 같다.

- `governance_plugin_tasks` 키가 없으면 `CONTRACT_MISMATCH`를 발생시킨다.
- 다음 경우에는 `INVALID_RESPONSE`를 발생시킨다.
  - 목록이 아니거나 항목이 Mapping이 아닌 경우
  - `task_id`가 빈 경우
  - `changed`가 bool이 아닌 경우
  - `changed_phases`가 문자열 목록이 아닌 경우

## AIK가 만들지 않는 것

- AIK 자체 gate 판정과 통과 승인
- gate·conformance 결과 cache
- 재시도
- gate를 생략하는 fallback
- Task 완료 권위

AGS gate는 VM Core 판정의 추가 조건으로 VM 안에서 집행된다. 이 모듈은 그 결과를 표시만 한다. 투영 결과는 진행 허가가 아니다. 특히 `None`도 "gate 통과"를 뜻하지 않으며, governance 차단으로 보고되지 않았다는 뜻일 뿐이다. gate와 conformance 결과의 재사용은 VM의 기존 공개 경로에 맡긴다.

## 공개 표면으로 얻을 수 없는 사실 (`UNREACHABLE_FACTS`)

| 이름 | 이유 |
|---|---|
| `contract_mismatch_phase` | `task.governance_blocked`의 phase는 History에만 기록된다 |
| `operation_no_effect` | `operation.no_effect`는 History에만 있다 |
| `conformance_check_list` | 적합성 검사 항목 전체 목록의 공개 읽기 메서드가 없다 |
| `gate_core_operations` | gate CoreOperation의 공개 읽기 메서드가 없다 |
| `governance_blocked_history` | governance 차단 History 목록의 공개 읽기 메서드가 없다 |

VM에는 History를 읽는 공개 API가 없다. `status()`와 `observe()`에도 governance 필드가 없다. AIK는 SQL `history_events`에 접근하지 않으며, 이 태스크에서 VM 계약 변경도 요청하지 않는다.

## 검증 상태

```bash
python -m unittest tests/test_vm_governance.py -v
```

시험은 합성 `BridgeReceipt`만 사용한다. VM·AGS 설치, provider, credential, 네트워크는 사용하지 않는다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

다음 항목은 **NOT_RUN**이다.

- 실제 설치형 AGS
- A2 profile
- 적합성 실행
- 호환 plugin과 manifest/관측 불일치의 설치형 검증
- 실제 VM `run_once`/`final_report`와의 결합

장부의 AIK-032 상태와 완료 판정은 변경하지 않으며, 이 모듈로 qualified를 주장하지 않는다.
