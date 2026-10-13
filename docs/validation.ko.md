# AIK-042 — AGS 독립검토와 VM 완료 판정 연결 (부분 구현)

`src/aik_validation/`은 한 VM Attempt에 관한 완료 관련 신호를 **서로 섞지 않고 출처와 함께** 표시하고, worker와 다른 reviewer 세션이 정책으로 고정된 검증 명령을 실행한 결과를 append-only로 기록한다. 이 패키지는 완료를 판정하지 않는다.

## 상태 분리

| `CompletionView` 필드 | 의미 | 출처(provenance) | 입력 |
| --- | --- | --- | --- |
| `provider_state` | provider job 상태 관측값 | `provider_observed` | `aik_observer` 관측(`vm_attempt_id`, `job_state`) |
| `dispatch_ack` | dispatch journal이 `sent_ack` 또는 `started`인지 | `local_derived` | `aik_dispatch.DispatchRecord` |
| `worker_claim` | worker가 보고한 완료 선언 원문 | `model_reported` | 호출자 |
| `independent_check` | 독립 검사 기록(`CheckRecord`) | `local_derived` | `IndependentChecker` |
| `vm_validation_result_id` | VM validation 결과 ID 원문 | `vm` | VM `run_once`/Attempt 응답 |
| `gate` | VM이 집행한 AGS gate 결과의 표시 투영 | `vm_run_once` | `aik_vm_bridge.governance.project_run_once` |
| `goal_verdict_id` | VM GoalVerdict ID 원문 | `vm` | VM `final_report` 응답 |
| `rework_reasons` | 후속 재작업 사유 원문 | `vm` | `failure_class`·`suggested_repair_action`·`blocker_code`·gate `reason_code` |

- `completion_authority`는 항상 `"vm"`이다.
- `vm_verdict_recorded`는 VM `final_report`가 `goal_verdict_id`를 줄 때만 `True`다. provider 성공, ACK, worker 완료 선언, 독립 검사 `PASS` 중 어느 것도, 그리고 그 조합도 이 값을 바꾸지 않는다. 기록된 GoalVerdict가 실패 판정일 수도 있으며, 통과 여부를 포함한 판정 내용 해석은 VM의 권한이다.
- VM 응답은 `aik_vm_bridge.BridgeReceipt`의 기대 command(`run_once`·`attempt_detail`·`final_report`)만 읽는다. Attempt 단위 응답은 같은 Attempt여야 하고(`ATTEMPT_MISMATCH`), `final_report`는 프로젝트 단위라 `vm_project_id`가 필요하며 같은 프로젝트여야 한다(`PROJECT_REQUIRED`·`PROJECT_MISMATCH`).
- `worker_claim`은 표시용 원문이며 어떤 상태로도 승격하지 않는다.
- 모든 입력은 같은 `vm_attempt_id`를 가리켜야 한다. VM 응답에 `attempt_id`가 없거나 다르면, 또는 gate 투영의 attempt가 다르면 `ValidationError(ATTEMPT_MISMATCH)`로 거절한다. `run_once`와 Attempt 응답의 validation ID가 서로 다르면 `VALIDATION_RESULT_CONFLICT`다. gate 투영이 실패하면 `GATE_PROJECTION_FAILED`로 끝나며 통과로 취급하지 않는다.
- 재작업 사유는 문자열을 바꾸지 않고 순서대로 회수하며 완전히 같은 값만 한 번으로 줄인다.

## 독립성 규칙

`IndependentChecker(tools, journal_path, clock=...)`의 `run(vm_attempt_id, worker_session_id=..., reviewer_session=..., invocation=..., command_id=...)`:

1. `reviewer_session.role`이 `reviewer`가 아니거나 `session_id`가 worker 세션과 같으면(대소문자 무시) **실행 전에** `NOT_INDEPENDENT`로 거절한다. 인가 요청과 명령은 시작되지 않는다.
2. 명령은 `aik_devtools.DevTools.run_verification`으로만 실행한다. argv·cwd·제한·oracle은 신뢰된 `DevToolPolicy`가 고정하며 worker나 reviewer가 바꿀 수 없다. 결과는 `DevTools.verify_result`로 같은 인스턴스가 만든 것인지 확인한다.
3. 판정: `passed and oracle_ok`일 때만 `PASS`, oracle 파일이 바뀌었으면(`oracle_ok=False`) `ORACLE_CHANGED`, 그 밖은 `FAIL`. worker가 `tests/expected.txt` 같은 판정 기준을 바꿔 명령이 0으로 끝나도 `ORACLE_CHANGED`다.
4. 기록에는 역할·reviewer/worker 세션·명령 ID·정의 digest·출력 digest·종료 코드·tool call ID·시각·provenance(`local_derived`)를 남긴다. 출력 본문은 저장하지 않는다.
5. 같은 Attempt의 재검사는 새 기록으로 추가한다. 이전 `FAIL` 기록은 지우거나 덮지 않으며 `history(vm_attempt_id)`가 추가 순서대로 반환한다. 같은 tool call의 동일 결과 재전달은 저장된 기록을 반환하고, 내용이 다르면 `TOOL_CALL_CONFLICT`다.

이 결과는 VM validation도 GoalVerdict도 아니다. VM에 넘길 **근거 후보**일 뿐이다.

## 저장소

`CheckJournal`은 별도 SQLite 파일(application ID `0x41494B56`, schema version 1)이다. `aik_runtime.paths`의 경로 검사(저장소·Git·개발 원장 거절, symlink/hardlink 거절)와 프로세스 잠금을 재사용한다. 기존 파일은 SQLite 헤더, application ID, 정확한 schema, `quick_check`를 확인하고, 다르면 `INVALID_JOURNAL`로 거절하며 파일을 바꾸지 않는다. UPDATE/DELETE는 trigger가 거절한다. 단, 파일에 직접 접근해 trigger를 제거하는 프로그램까지 막지는 않는다. 연결은 작업마다 열고 닫는다.

## VM 권위 유지

VM이 Task·Attempt·재시도·검증·GoalVerdict의 권위이고 AGS gate는 VM이 집행한다. 이 패키지는 gate를 생략·대체하지 않고, VM 결과를 재해석하지 않으며, 기존 인가 범위를 넓히지 않는다(검사 명령도 기존 authority gateway를 거친다).

## 검증 범위

```bash
python -m unittest tests/test_validation.py -v
```

시험은 임시 디렉터리, 합성 데이터, 주입 시계만 쓴다. DevTools는 실제 자식 프로세스로 실행하지만 authority는 fake다. VM 응답은 합성 payload를 담은 실제 `BridgeReceipt` 객체이고, governance 투영은 실제 `project_run_once`를 거친다. 투영 실패 경로 한 시험만 투영 함수를 실패하도록 바꾼다.

**NOT_RUN (미검증):**

- VM Goal Test·재작업 루프와의 실제 결합(VM이 이 근거 후보를 받아 재작업을 지시하고 GoalVerdict를 내는 과정).
- 실제 VM이 반환한 `BridgeReceipt`와의 결합. 시험은 합성 payload를 담은 실제 `BridgeReceipt` 객체를 쓴다.
- AGS 독립검토의 실제 실행과 실제 authority gateway에서 reviewer 역할의 검증 도구 인가.
- Windows 동작. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

장부 상태와 완료 판정은 변경하지 않는다. 이 구현은 qualified가 아니다.
