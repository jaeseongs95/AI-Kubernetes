# AIK-011 — 인증 전환과 실행 경합 보강

기존 `ProviderAdapter` 공통 계약은 유지하고 `GuardedAdapter`의 인증 전환·재진입·불명확 결과 처리만 보강한다. 새로운 provider API나 Task/Attempt 원장을 만들지 않는다. [기존 계약](provider-adapter.ko.md)의 실제 호스트·제품 통합 한계도 유지한다.

## 인증 문맥의 점유

로그인/로그아웃 호출 전에 원래 `ConnectionBinding`과 당시 isolation을 예약한다. `pending` 응답은 완료가 아니므로 단일 활성 계정 호스트의 다른 인증 변경과 실행을 차단한다. 같은 요청을 다시 로그인으로 전달하지 않는다. 격리된 호스트도 같은 connection의 활성 작업이나 인증 전환 중에는 그 connection을 다시 인증하거나 다른 revision으로 해제할 수 없다.

원래 connection/revision/account가 일치하는 관측만 예약을 해제한다. 로그인은 `authenticated`, 로그아웃은 `unauthenticated`가 필요하다. 로그인 중 `unauthenticated`, 로그아웃 중 `authenticated`를 읽었다고 진행 중 작업이 끝났다고 판정하지 않는다. 다른 revision/계정의 상태 조회는 기존 예약에 영향을 주지 않는다. 원래 예약이 공유 문맥이었다면 이후 isolation 광고가 바뀌어도 진행 중 범위를 넓히지 않는다.

`auth_status(original_binding)`으로 완료를 관측할 수 있다. 같은 binding의 `start_job`도 읽기 전용 인증 조회를 통해 완료된 로그인을 확인한 뒤 진행할 수 있다. 이 경로에서도 미완료 로그아웃·잘못된 계정·미지원 상태는 실행을 막는다. 실제 호스트 adapter가 해당 상태를 권위 있게 관측해야 하며 임의 fixture 값을 실측으로 사용하면 안 된다.

## 불명확한 결과와 재호출

구조화된 `AdapterFailure`라도 `outcome=unknown`이면 인증 변경 결과가 불명확하므로 host fence를 유지한다. 잘못된 응답 identity와 원시 예외도 같은 방식으로 차단한다. 명시적인 `known_failure`는 해당 인증 예약만 해제할 수 있다.

시작·중단 응답 자체의 job state가 `unknown`인 경우도 후속 효과를 차단한다. 중단이 타임아웃 난 뒤 같은 중단을 다시 보내지 않는다. 원래 binding으로 읽기 전용 `observe_job`은 계속 사용할 수 있지만, 정상 인증 조회나 terminal job 하나를 봤다는 이유로 host의 불명확한 다른 효과를 자동 해제하지 않는다. 영속 원장 기반 reconciliation은 기존 후행 통합 태스크의 책임이다.

## callback 재진입

기존 `RLock`은 같은 thread의 재진입을 허용하므로 인가/provider callback이 바깥 실행의 기록 전에 새 실행을 시작할 수 있었다. provider-facing 작업의 재진입을 명시적으로 거절하고, `get`·`unresolved`·`uncertain_hosts` 같은 로컬 조회는 유지했다. 외부 인가 callback 후에는 계정과 capability를 다시 조회해 callback 전의 오래된 관측만으로 실행하지 않는다.

이 검사는 하나의 동기 `GuardedAdapter` 인스턴스 범위다. 별도 프로세스, 다른 guard, 원문 호스트 로그인, 검증 뒤 외부 변경을 OS 수준에서 격리하지 않는다. 기본 인가 거절, API 키/구독 세션 참조 분리, 실제 qualification의 별도 책임은 그대로다.

## 검증·원장

```bash
python -m unittest discover -s tests -p "test_provider*.py" -v
python tools/backlog.py validate
```

기존 36개 테스트를 변경하지 않고 신규 24개를 추가했다. 선택 반례 8개를 수정 전 controller에서 별도로 실행하고 수정 후 전체 suite를 검사한다. 결과는 [실행 근거](../evidence/provider-adapter/2026-10-13-lifecycle/README.ko.md)와 같은 판본의 CI에 결속한다.

AIK-011의 결함 수정 근거와 상태 이력만 정본 SQLite에 기록한다. AIK-010 및 실제 공식 호스트/제품 연동이 미완료이므로 전체 태스크는 `blocked`로 기록하며, 다른 태스크·완료 기준·의존성·지원 matrix를 바꾸지 않는다.
