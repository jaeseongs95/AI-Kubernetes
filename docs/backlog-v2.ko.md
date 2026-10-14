# 개발 원장 schema 2

2026-10-14에 개발 원장(`planning/backlog.sqlite3`)을 schema 1에서 schema 2로 바꿨다. 이유는 사용자가 정한 세 가지 설계 원칙과 첫 출시 범위 변경을 원장이 직접 표현하고 검증하게 하려는 것이다.

> 사용자 원문: "나의 철학은 1. 밴더 독립은 무조건 지킨다 2. 운영체제에도 독립적이어야한다 3. SOLID 원칙을 좋아한다"

## 사용자 결정과 해석

| 결정 | 원장 반영 |
|---|---|
| 출시 OS는 Windows·Linux 둘 다 | 첫 출시 완료 조건에 Windows x64(`WIN-X64`)와 Linux x64(`LINUX-X64`)를 모두 넣는다(U04, DEC01). 진행은 Windows가 먼저다. |
| Linux의 VM 실행 의존 부분 | VM RuntimeJob owner lock이 Windows 전용이다(VM 결정 D1, POSIX는 fail-closed). VM은 고정 계약이므로 외부 선행 `EXT-VM-POSIX-D1`로 두고, 관련 작업의 `LINUX-X64` 행을 차단한다. |
| Linux 비밀 저장 | Secret Service(`secret-tool`, D-Bus 키링). 키링이 없으면 저장을 거절한다(DEC09). Windows 쪽 선택은 아직 제안 상태다. |
| 개편 깊이 | 스키마까지 바꾼다(schema 2). |

## 스키마 변경

schema 1의 테이블·열·제약·뷰는 그대로 두고 다음을 추가했다. 정본 정의는 [`planning/schema.sql`](../planning/schema.sql)이다. schema 1 정의는 변환 원본을 검증하는 데에만 쓰며 [`planning/schema-v1.sql`](../planning/schema-v1.sql)에 보존한다.

| 추가 | 용도 |
|---|---|
| `principles`, `task_principles` | 설계 원칙 `PRN-VENDOR`·`PRN-OS`·`PRN-SOLID`와 작업 연결 |
| `platforms` | 지원 플랫폼(계열, 출시 범위) |
| `task_platforms` | 작업별 플랫폼 상태. `tasks.status`는 전체 상태이고 이 표가 플랫폼별 상태다. 상태 값은 작업 상태에 `not_applicable`을 더한 것이다. |
| `external_dependencies`, `task_external_dependencies` | 수정할 수 없는 외부 계약(VM·AGS 등)의 미해소 선행과 그에 막힌 작업·플랫폼 |
| `change_log` | 요구·결정·마일스톤·작업 문구와 연결 변경의 이전·이후 값. 각 행이 앞 행의 digest를 이어받는 추가 전용 기록이다. |
| `task_evidence.platform_id` | 근거가 관측된 플랫폼. NULL은 플랫폼 무관이다. |
| `v_platform_summary`, `v_open_external` | 단계×플랫폼 상태 수, 미해소 외부 선행과 막힌 작업 |

`change_log`의 추가 전용 성질은 trigger가 아니라 digest 연쇄 검사로 지킨다. 복원(`restore`)은 보안상 SQL 스냅샷의 trigger 생성을 거절하기 때문이다. 작업 이력 검사와 마찬가지로 도구 밖의 수정·삭제를 찾아내는 장치이며, 연쇄를 다시 계산하는 조작까지 막지는 않는다.

## 검증 규칙 추가 (`tools/backlog.py validate`)

1. 완료 작업은 모든 플랫폼 행이 `done` 또는 `not_applicable`이어야 한다.
2. `todo` 작업의 플랫폼 행은 `in_progress`나 `done`일 수 없다.
3. 플랫폼 행이 `done`이면 그 플랫폼의 근거나 플랫폼 무관 근거가 있어야 한다.
4. `blocked`·`not_applicable`·`cancelled` 플랫폼 행에는 이유(note)가 있어야 한다.
5. 원칙이 하나라도 기록돼 있으면 모든 feature·integration 작업에 원칙이 연결돼야 하고, 모든 원칙에 작업이 있어야 한다.
6. 미해소 외부 선행에 걸린 작업·플랫폼 행은 완료할 수 없다.
7. 첫 출시 단계의 작업은 첫 출시 플랫폼마다 플랫폼 행이 있어야 한다.
8. 플랫폼을 지정한 외부 선행에는 그 플랫폼 행이 있어야 한다.
9. `change_log`의 digest 연쇄가 끊기지 않아야 한다.
10. 플랫폼 행이 `in_progress` 또는 `done`이면 같은 플랫폼의 모든 내부 선행 행이 `done` 또는 이유가 기록된 `not_applicable`이어야 한다. 선행 작업에 플랫폼 행이 전혀 없을 때만 전체 `done`을 플랫폼 무관 근거로 사용한다. 다른 플랫폼의 완료나 누락된 플랫폼 행으로 대체하지 않는다.
11. 플랫폼의 미해소 외부 선행은 `done`뿐 아니라 `in_progress`도 차단한다. 전체 작업 완료에 필요한 양쪽 플랫폼 조건은 그대로 유지한다.

## 도구 변경

- `migrate`: 유효한 schema 1 DB를 schema 2로 바꾼다.
  - schema 1 행은 모두 그대로 복사한다. 바뀌는 것은 `metadata.schema_version`과 `updated_at`뿐이고, 그 변경을 `change_log` 첫 행에 남긴다.
  - 새 파일에서 검증한 뒤 원자적으로 교체하며, 기본 DB이면 SQL 스냅샷과 보고서도 함께 갱신한다.
  - `--output`을 주면 새 파일로 쓴다. `--at`으로 시각을 고정하면 같은 입력에서 바이트까지 같은 결과가 나온다.
  - 이미 schema 2인 DB에는 아무것도 바꾸지 않는다.
- `status 작업 상태 --platform 플랫폼 --note 이유`: 플랫폼 행만 바꾼다. `change_log`와 작업 이력(`platform_status` 사건)에 함께 남긴다.
- `report`: 설계 원칙, 플랫폼별 현황, 외부 선행, 변경 이력 절을 추가하고 작업마다 원칙·플랫폼 상태·외부 선행을 표시한다.
- `list --ready --platform WIN-X64` / `LINUX-X64`: 해당 플랫폼의 상태·내부 선행·외부 선행으로 착수 가능 작업을 조회한다. `--platform`을 생략한 조회와 전체 상태 변경은 기존 전체 작업 기준을 유지한다.

전체 `todo`에 플랫폼 진행 행을 두지 않는 기존 규칙도 유지한다. 한 플랫폼이 준비되었지만 다른 플랫폼의 선행 때문에 전체 작업은 진행할 수 없다면, 전체 상태를 이유와 함께 `blocked`로 기록한 뒤 준비된 플랫폼만 `--platform`으로 시작한다. 플랫폼별 준비 조회는 전체 `in_progress`·`done`의 선행 조건을 면제하지 않으며, 전체 취소 작업은 준비 목록에 나오지 않는다.

## 내용 개편 (2026-10-14, 한 트랜잭션)

작업 상태는 하나도 바꾸지 않았다. 기존 작업 90개, 작업 사건 308건, 근거 82건은 모두 보존했다. 모든 문구 변경은 `change_log`에 이전·이후 값으로 남아 있다.

- **원칙·출처**: `S-USER-20261014`과 원칙 3개를 추가했다.
- **요구**
  - U04를 "첫 출시 Windows·Linux"로 바꿨다.
  - U10(OS 독립 구조)과 U11(벤더 독립 구조)을 새로 만들었다.
  - D01·D07과 P01~P12의 Windows 한정 문구를 지원 플랫폼 기준으로 바꿨다.
- **결정**
  - DEC01·06·08·09·13을 고쳤다.
  - DEC14(OS·벤더 port 경계, SOLID)를 confirmed로 추가했다.
  - DEC08의 Linux 배포판과 설치 형식은 아직 정하지 않았다.
- **단계·에픽**
  - 이름을 바꿨다: M0 "통합 계약·플랫폼 기반 결정", M3 "데스크톱 GUI 실제 연결", M4 "첫 배포 검증(Windows·Linux)", E08 "데스크톱 GUI", E09 "출시·패키징".
  - E12 "플랫폼 독립 계층"을 새로 만들었다.
- **작업 문구**
  - Windows 전용으로 적힌 제목·본문·완료 기준을 지원 플랫폼 기준으로 바꿨다.
  - AIK-002는 이미 검증한 기준을 바꾸지 않았다. 제목에만 "(Windows 실측)"을 붙였다.
- **새 작업**
  - AIK-091 "OS port 계층·플랫폼 adapter와 공통 헬퍼 통합"(M0, 지금 착수 가능)
  - AIK-092 "공통 코드의 벤더 결합 제거"(M1)
  - Linux 시크릿 backend, 세션 잠금 신호, 패키징, 설치본 E2E는 별도 작업으로 만들지 않았다. 기존 작업(009, 016, 003·057, 059)의 `LINUX-X64` 행이 그 일을 맡는다.
- **플랫폼 행**: 첫 출시 작업 74개 × 2 = 148행이다.
  - 기본은 작업 상태를 그대로 따른다. blocked 작업은 이유를 적고 blocked로 둔다.
  - AIK-001·002는 `WIN-X64`를 done으로 두고, `LINUX-X64`는 이유를 적어 not_applicable로 뒀다. Linux에서 다시 확인할 일은 후속 작업의 Linux 행이 맡는다.
  - VM 실제 실행에 의존하는 AIK-031·044·059·060·064·086의 `LINUX-X64` 행은 `EXT-VM-POSIX-D1`에 막혀 blocked다.
  - AIK-081·086은 `EXT-AGS-A2-HOSTINTEGRATION`에 연결했다.
- **근거 플랫폼**: 근거 요약에 Windows 실행 환경이 적힌 10건만 `WIN-X64`로 지정했다. 나머지는 추정하지 않고 NULL로 뒀다.
- **원칙 연결**
  - `PRN-SOLID`는 모든 feature·integration 작업에 붙였다.
  - `PRN-VENDOR`는 E02·E03·E04 작업과 벤더 경계 작업에 붙였다.
  - `PRN-OS`는 E01·E08·E09·E12 작업과 OS 경계 작업에 붙였다.

## 검증

- **migrate 결과**: 실제 원장 사본을 `--at`으로 두 번 변환하자 결과 파일이 바이트까지 같았다.
- **schema 1 행 보존**: 표 14개 모두 schema 1과 일치했다. 다른 값은 `metadata`의 `schema_version`과 `updated_at` 두 개뿐이고, `task_events` 자동 증가 값(308)도 같다.
- **SQL 스냅샷 왕복**: export → restore → validate가 통과했다.
- **시험**: `tests/test_backlog_migration.py`(6건), `tests/test_backlog_validation.py`(v2 규칙 시험 12건 추가, 기존 시험도 v2 fixture에서 다시 실행)
- **전체 회귀**: `python -m unittest discover -s tests`
