# 실제 제품 검증 착수 결과 — 2026-10-12

## 판정

**실제 제품 검증은 실행 대상 부재로 차단됐다. 제품 시나리오 실행은 0건이며, 제품 기능의 통과 또는 실패를 관측한 결과는 없다.**

검사한 `main` 커밋은 `641d99d93169db182fc7c4f66f377ffb146a5067`, Git tree는 `115d58dbd28f74440b521de434b6f8109f50a7a5`다. 이 판정은 해당 시점의 저장소와 게시된 배포물을 대상으로 한다. 이 보고서를 추가한 후의 파일 수와 과거 검사 대상의 파일 수를 혼동하지 않는다.

현재 저장소에는 설계 문서, SQLite 개발 장부, 장부 관리 CLI와 시험만 있다. Windows GUI·시크릿 관리·모델 지정·AIK/AGS/VM 연결을 실행할 제품 코드, 빌드 진입점, 설치본은 없다. README도 제품 구현 전 상태를 명시한다. 따라서 이번 요청을 제품 E2E 통과로 보고할 수 없다.

상세 관측값, 조회 URL, 파일 해시, 선행 태스크 전체 목록은 [기계 판독용 기록](../planning/runtime-validation.json)에 있다.

## 실제 수행한 확인

| 확인 대상 | 관측 결과 |
|---|---|
| 원격 브랜치 | `main` 1개, 위 커밋과 일치 |
| 전체 Git tag refs / Releases | 각각 0개 |
| GitHub Actions 실행 이력 | `total_count: 0` |
| 원격 전체 Git tree | 누락 없는 응답(`truncated: false`), 게시 파일 17개 |
| 원격과 로컬 파일 대조 | 17개 모두 전체 바이트로 계산한 Git blob 해시 일치 |
| 실행 코드 | `tools/backlog.py`와 시험 파일 4개; 모두 장부 또는 계획 검증 용도 |
| SQLite 읽기 전용 조회 | `integrity_check: ok`, 외래 키 위반 0건, 90개 태스크 모두 `todo` |
| 제품 완료 근거 | `task_evidence` 0건 |
| 의존성 계산 | 아래 실제 검증 경로의 모든 선행 태스크가 `todo` |
| 현재 실행 환경 | Linux x86_64, Python·Node·Git 확인; 네이티브 Windows 실행은 수행하지 않음 |

저장소 전체 workflows/artifacts 목록 API는 연결 도구에서 지원하지 않아 그 API 개수를 0으로 기록하지 않았다. 현재 커밋에 workflow 소스가 없다는 사실은 전체 Git tree로 확인했다. 계정·시크릿 준비 여부는 검사하지 않았으며 **미확인**이다. 별도 미게시 구현이나 개인 Windows PC 파일은 확인 범위 밖이다.

SQLite SHA-256은 검사 전후 모두 `e17ac9d22f6cca6ebb0341d6b14cd9a3677c97ff17fec4ddc95b7c5485410df0`다.

## 실제 실행 경로별 결과

`NOT_RUN`은 제품 실행 전 차단을 뜻한다. 제품을 실행해 기능 오류를 관측한 `FAIL`과 구분한다. 선행 수는 전이 의존성 전체이며 해당 검증 태스크 자신은 제외한다.

| 태스크 | 검증할 실제 동작 | 현재 없는 실행 대상 | 미완료 선행 수 | 결과 |
|---|---|---|---:|---|
| AIK-008 | 설치된 AGS/VM과 AIK의 적합성 진단 | AIK 진단 구현·설치형 통합 대상 | 4 | NOT_RUN |
| AIK-082 | 벤더별 최소 실제 호출과 관측 | API/native adapter와 계정 결속 구현 | 18 | NOT_RUN |
| AIK-086 | 인가·lease·workspace·도구·관측·복구를 거치는 실제 실행 | AIK 통합 runtime 패키지·실행 진입점 | 44 | NOT_RUN |
| AIK-044 | 실제 총괄·두 벤더의 A/B→C 개발 작업과 재검증 | 총괄과 통합 작업 실행 루프 | 49 | NOT_RUN |
| AIK-059 | Windows 설치본의 GUI 사용자 여정 | GUI 구현·Windows 설치본·runtime bundle | 66 | NOT_RUN |
| AIK-064 | 동일 설치본의 품질 근거에 따른 첫 출시 판정 | 실행된 인수·장애·비밀·성능 검증 근거 | 71 | NOT_RUN / 출시 NO-GO |

첫 Windows 제품의 P01~P10도 모두 `NOT_RUN`이다. 여기에는 멀티벤더 인증 수명주기, 프로젝트 선택, 실제 역할별 모델 사용, 의존 개발 작업, 인가·비밀·예산, 중단 후 회복, 결과 인계, GUI 감독, 유지보수 복원, 고정 설치본의 출시 품질이 포함된다. 각 요구사항과 태스크 연결은 JSON에 보존했다.

첫 차단 원인은 제품 구현 부재다. Windows 환경이나 계정만 준비해도 즉시 제품 시험을 시작할 수 있는 상태는 아니다.

## 기존 65개 시험과의 관계

`planning/validation.json`에 기록한 **65개 PASS는 2026-10-12 06:58:29 UTC의 Linux 장부 도구·계획 구조 검증**이다. 이 기록은 당시 실행 결과로 유지한다. 이번 조사에서 해당 시험을 다시 실행하지 않았고, 그 통과 수를 제품 실행 실적으로 합산하지 않았다.

이번에 직접 실행한 것은 저장소 조회, 파일 대조, SQLite 조회와 선행 집합 계산, 환경 확인이다. AIK 제품·Windows 설치본·실제 벤더 호출·AGS/VM 제품 코드를 실행한 건수는 0이다. 장부는 90개 `todo`와 제품 완료 근거 0건을 유지한다.

## 실제 검증으로 진행하기 위한 기존 작업

의존성 기준으로 지금 착수 가능한 태스크는 다음 세 개다.

1. **AIK-001:** 실제 AGS/VM 설치물과 공개 인터페이스의 계약·지원 기준선 확정.
2. **AIK-002:** Windows GUI 스택과 엔진 sidecar의 실제 설치·IPC·worker 기동 검증.
3. **AIK-003:** Windows 지원 환경과 설치·서명 방식 결정 및 설치 spike.

이후 장부의 선행 순서대로 기반·인증·adapter를 구현하면 008·082의 실제 관측에 진입한다. 작업 실행·workspace·도구·lease·복구와 설치형 통합을 완성해야 086과 044를 수행할 수 있다. GUI와 배포 구성을 구현한 뒤 059·060·061·062·063·064에서 동일한 Windows 설치본의 출시 여부를 판단한다. 이 문단은 기존 경로를 압축한 설명이며, 정확한 실행 순서는 SQLite 의존성과 JSON의 전체 선행 목록을 따른다.

추가 태스크나 완료 기준을 만들지 않았다. 현재 필요한 것은 기존 태스크의 구현과 그 산출물에 대한 실행이다. **태스크대로 개발하면 완제품이 나온다는 주장은 실제 설치본과 통합 검증을 통과하기 전까지 실증되지 않은 상태다.**

## 재현 방법

위 고정 커밋의 저장소에서 다음 읽기 전용 조회로 장부 상태를 다시 확인할 수 있다.

```python
import hashlib
import sqlite3
from pathlib import Path

database = Path('planning/backlog.sqlite3').resolve()
print('sha256:', hashlib.sha256(database.read_bytes()).hexdigest())
connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
print('integrity:', connection.execute('PRAGMA integrity_check').fetchall())
print('foreign_keys:', connection.execute('PRAGMA foreign_key_check').fetchall())
print('statuses:', connection.execute(
    'SELECT status, count(*) FROM tasks GROUP BY status'
).fetchall())
print('evidence:', connection.execute('SELECT count(*) FROM task_evidence').fetchone())
connection.close()
```

원격 배포물 재조회에는 JSON에 기록한 GitHub GET URL을 사용한다. 커밋을 바꾸거나 새 구현·배포물이 추가된 경우에는 해당 새 버전을 대상으로 실행 대상부터 다시 확인해야 한다.
