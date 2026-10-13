# AIK-019 — ResourcePool 레지스트리·건강 상태·발견

`src/aik_pool/`은 AIK-011 공통 adapter 계약(`aik_provider`)과 AIK-005 `aik_runtime.Pool` 모델을 그대로 재사용한다. 두 패키지는 수정하지 않는다. 이 패키지는 pool·model 경로별로 다음 세 가지를 노출하는 메모리 inventory다.

- 모델·host·tools
- 현재 기술적 실행 가능 상태
- qualification 단계

표준 라이브러리만 사용한다. 실제 provider 호출, 작업 시작·중단, 로그인·로그아웃은 하지 않는다.

## 경로와 식별자

경로(route)는 `(aik_runtime.Pool, model)` 한 쌍이다. 각 경로는 다음 식별자를 별도로 갖는다.

- `pool_id`: AIK 설정
- `connection_id`와 `connection_revision`: 공통 `ConnectionBinding`
- `auth_kind`: `api_key` 또는 `native_subscription`

같은 모델을 구독 세션과 API 키 두 경로로 등록하면, 서로 다른 pool·connection으로 각각 조회된다. 조회 방법은 `routes(model=...)`, `routes(pool_id=...)`, `routes(connection_id=...)`이다.

등록 시 다음 조건을 검사한다. 하나라도 어긋나면 고정 코드의 `PoolRegistryError`로 거절한다.

- `Pool.connection_id`와 `Pool.provider`가 binding과 일치해야 한다.
- 같은 `connection_id`를 쓰는 경로들의 `ConnectionBinding`은 전체가 동일해야 한다(revision뿐 아니라 provider·runtime·auth_kind·credential 참조·계정 fingerprint 포함). 다르면 `CONNECTION_REVISION_CONFLICT`다.
- 같은 `pool_id`를 쓰는 경로들은 `Pool`과 binding이 모두 동일해야 한다(`POOL_CONFLICT`).

`tools`는 등록 시 선언된 식별자다. 공통 adapter 계약에 도구 probe가 없으므로, 관측해서 확인한 값이 아니다.

## 별도 필드

`RouteStatus`는 아래 값을 서로 다른 필드로 둔다.

| 필드 | 의미 |
| --- | --- |
| `implementation_state` | adapter가 밝힌 구현 여부(`implemented`/`not_implemented`/`unknown`). TTL이 지나면 `stale` |
| `account_binding` | `bound`, `account_changed`, `unauthenticated`, `pending`, `expired`, `locked`, `stale_binding`, `unknown`, `stale` |
| `runtime_support` | auth_status·job_start·job_observe·job_interrupt가 모두 supported일 때만 `supported`. 하나라도 unsupported면 `unsupported`, 나머지는 `unknown`. native 구독의 isolation이 unknown이면 `unknown` |
| `governance` | 외부에서 설정한 `approved`/`rejected`/`unknown`(기본 unknown) |
| `last_probe_at` / `last_verified_at` | 마지막 probe 시각 / probe에서 계정이 `bound`이고 runtime이 `supported`로 확인된 마지막 시각. 로그아웃·pending·계정 변경·runtime 미지원을 관측한 probe는 이 값을 갱신하지 않는다 |
| `matrix_revision` / `qualification_matrix_revision` | 현재 지원 matrix revision / 유효한 qualification 기록의 revision |
| `capacity_state` / `available_capacity` | `known`·`unknown`·`stale` / 관측 정수 또는 `None` |
| `level` | 아래 qualification 단계 |
| `executable_now` / `placement_eligible` | probe 기준 기술적 가용성 / 자동 배치 광고 가능 여부 |

`executable_now`는 다음 조건을 모두 만족해야 참이다. 따라서 구현 완료나 등록만으로는 실행 가능으로 광고되지 않는다.

- `level`이 `probe_verified`·`candidate_for_qualification`·`qualified` 중 하나이다. 즉 신선한 probe에서 `implementation_state`가 `implemented`, 계정이 `bound`, runtime이 `supported`이다.
- governance가 `rejected`가 아니다.
- pool이 `enabled`이다.
- 관측된 가용 용량이 알려져 있고 1 이상이다.

`executable_now`는 **실행 인가가 아니다.** governance가 `unknown`인 경로도 기술적으로 가용하면 참이 될 수 있다. 일반 운영과 자동 배치는 `placement_eligible`(=`executable_now`이고 `qualified`)만 사용하며, `operational_inventory()`도 이 값만 본다. `routes()`는 진단용이다.

## 단계 구분

- `implemented`: adapter가 구현을 밝혔다. 인증·runtime은 확인되지 않았거나 실패했다.
- `probe_verified`: 신선한 probe에서 계정 binding과 runtime 지원을 확인했다.
- `candidate_for_qualification`: 위 조건에 더해 governance가 `approved`이다.
- `qualified`: 위 조건에 더해, 외부에서 주입한 `qualification_verifier`가 승인한 live 기록이 있다. 이 기록은 현재 binding revision, 계정 fingerprint, runtime, matrix revision과 일치해야 한다.

`record_qualification`은 다음 경우를 `QualificationRejected`로 거절한다. 기본값(verifier 없음)은 항상 거절이다.

- `evidence_kind != "live"`
- `verdict != "PASS"`
- binding·matrix 불일치
- candidate가 아닌 경로
- verifier가 없거나, 예외를 내거나, 정확히 `True`가 아닌 값을 반환

## 재검증과 덮어쓰기 방지

다음 probe 결과 중 하나라도 해당하면 그 경로의 qualification 기록을 버린다. 이후에는 다시 qualification을 받아야 한다.

- 계정 관측이 `bound`가 아님(외부 로그인 변경 `account_changed`, 로그아웃, 인증 상태 불명 등)
- runtime이 `supported`가 아님
- `implementation_state`가 `implemented`가 아님

`update_binding()`으로 새 connection revision을 반영하면 관측·검증 시각·qualification을 모두 초기화하므로 다시 probe해야 한다. 이때 governance `approved`는 `unknown`으로 되돌아가며 다시 승인해야 한다. `rejected`는 유지한다.

matrix revision을 올리면 모든 qualification 기록이 무효가 되고, 모든 경로의 governance `approved`도 `unknown`으로 되돌아간다(`rejected`는 유지). revision을 낮출 수는 없다.

상태는 경로마다 따로 계산한다. 한 경로가 `unsupported`이거나 계정이 바뀌어도, 같은 모델의 다른 connection·다른 계정 경로의 단계(`qualified` 포함)는 바뀌지 않는다.

`register`, `update_binding`, `update_pool`, `set_governance`는 상태를 바꾸기 전에 시계를 읽는다. 시계 예외(`CLOCK_UNAVAILABLE`)나 timezone 없는 값(`INVALID_CLOCK`)이면 레지스트리 상태는 바뀌지 않고, 같은 호출을 다시 시도할 수 있다.

## TTL·stale·용량

시계는 `clock`으로 주입할 수 있다. 반환값은 timezone 있는 `datetime`이어야 한다.

- 마지막 probe 이후 `ttl_seconds`(기본 300초)를 넘었거나 시계가 역행하면 `stale`로 표시한다.
- `stale` 상태에서는 구현 여부·계정·runtime·용량이 모두 `stale`, 용량 값이 `None`이 되고, 단계는 `unknown`이다. `last_probe_at`은 과거 관측 시각을 그대로 보여 준다.
- `refresh()`는 stale이거나 한 번도 probe하지 않은 경로만 다시 probe한다. `refresh(stale_only=False)`는 모든 경로를 다시 probe한다.
- 같은 binding·계정으로 다시 probe해 신선해지고 위의 무효화 조건에 해당하지 않으면, 기존에 유효했던 qualification 기록은 다시 반영된다.

용량은 선택 사항인 `capacity_probe(pool_id, connection_id, model)`가 반환한 `0..Pool.max_parallel` 정수만 `known`으로 받는다. 다음 경우는 모두 `unknown`/`None`이며, 0이나 무제한으로 변환하지 않는다.

- probe가 없음
- `None`, bool, 음수, 실수, 최대치 초과
- 예외
- `update_pool`로 `max_parallel`을 이미 관측한 용량보다 낮춘 경우

관측된 0은 `known`이며 실행 불가로 표시한다.

## 검증

```powershell
python -m unittest discover -s tests -p 'test_pool.py' -v
```

시험은 `tests/test_pool.py`의 공통 fake adapter와 fake clock만 사용한다. fake adapter의 작업·인증 변경 메서드가 한 번도 호출되지 않았는지도 확인한다. 검사 항목은 다음과 같다.

- 다중 계정과 구독/API 경로 분리
- 외부 로그인 변경, pending·unknown isolation, 그 밖의 인증 상태, auth_status 예외 원문 비노출, unsupported 격리
- governance `rejected`의 `executable_now` 차단, binding·matrix 변경 시 승인 회수
- TTL 만료·시계 역행, stale 구현 상태, 용량 unknown, 시계 실패 시 상태 불변
- qualification 거절 경로, 입력 검증과 고정 오류 코드
- `qualified` 전이 로직과 그 무효화(계정 변경, runtime 상실, matrix 상승, binding 갱신, stale 후 재반영, 다른 경로 unsupported의 비간섭)

`qualified` 전이 로직 시험은 프로세스 메모리 안의 합성 verifier(`lambda e: True`)와 `evidence_kind="live"` 표지를 쓴다. 이것은 레지스트리 로직의 단위시험일 뿐 live 근거가 아니다. 그 결과를 어디에도 기록하지 않으며, 실제 경로가 qualified라는 주장의 근거로 쓰지 않는다.

## 한계·미검증(NOT_RUN)

- 실제 live qualification은 근거가 없으므로 실행하지 않았다. fixture로 live 근거를 만들지 않는다. 단위시험의 합성 verifier는 위에서 설명한 로직 검증에 한정된다.
- 실제 provider, 공식 호스트 로그인, Windows 11 동작은 미검증이다. Linux 단위시험 통과는 Windows 지원 증거가 아니다.
- 외부 로그인 변경은 probe 시점에만 감지된다. TTL 안에서 probe하지 않으면 반영되지 않는다.
- inventory는 프로세스 메모리에만 있다. 재시작 후에는 다시 등록하고 다시 probe해야 한다. 영속화, 실제 배치·lease, VM/AGS 인가는 후행 태스크 범위다.
- probe는 레지스트리 lock을 잡은 채 adapter를 호출한다. 느린 provider는 같은 레지스트리의 다른 호출을 지연시킨다.
- 장부 상태와 완료 판정은 변경하지 않는다.
