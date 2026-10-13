# AIK-083 — 파일 oracle snapshot 회귀 근거

단일 대상 태스크는 **AIK-083**이다. 기준 main은 `b60f7c71c96dd0d95fa98214d744db04dd65e562`, 이전 검사기 Git blob은 `766a91bf50596377b5a72bcb301d6396a1b78b34`다. 기존 12개 시험·인수계약·고정 프로젝트·지원 matrix는 변경하지 않았다.

## 실제 원장 조회·이전 결함 재현

검증 소스 `387aa4b32f2cbbfc60739890a29c04e2272abff3`의 [실행 38356398778](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38356398778)에서 실제 `planning/backlog.sqlite3`를 읽었다. AIK-083은 todo, 선행 AIK-001/002는 done, AIK-003은 blocked였으며 기존 완료 기준 네 개를 보존했다.

`record_evidence.py`는 고정 이전 검사기를 별도 임시 디렉터리에 복원하고 신규 선택 반례8개를 실행했다. **실패6개·오류2개, 통과0개**를 양쪽 OS에서 재현했다. 검증 후 경로 재읽기로 다른 바이트의 digest를 발행하는 결함, 입력 hash 확인 뒤 바뀐 입력을 계산에 사용하는 결함, object 키 순서 오판, 무제한 파일 읽기, 비정상 JSON 숫자/문자열/중첩과 비정규 파일의 오류 경계를 구분했다. 모든 값은 합성 테스트이며 실제 provider/계정은 호출하지 않는다.

## 수정본과 판본 결속

같은 실행에서 Windows Server 2022 및 Ubuntu 24.04/Python 3.14.8로 **기존12+신규24=36/36 PASS**와 원장 회귀 **53/53 PASS**, 정본 검증이 통과했다.

준비 실행에서는 `apply_checker_patch.py`가 고정 이전 바이트에서 새 검사기를 결정적으로 생성한 뒤 검사했다. 따라서 branch commit의 이전 `tools/product_acceptance.py`와 생성된 검사기를 혼동하지 않는다. 생성된 검사기의 정확한 Git blob은 `3a3918a7e2c1b1331d0a405684481a8c6c0acf0a`, SHA-256은 `342cd2b7da9d10909c1298e854cadfbb37db3db677906de6a137fd16f58ebec6`이다. 최종 커밋에는 이 검증된 blob을 그대로 넣고, 최종 PR/메인 CI에서는 파일 생성 단계 없이 커밋된 검사기를 직접 시험한다. 로컬 실행했다고 기록하지 않는다.

## Windows에서 발견한 추가 차이와 보정

첫 실행 `38355756448`에서는 TEMP의 8.3 별칭 때문에 파일 변경 주입 테스트 한 건이 잘못된 경로를 비교했다. 테스트 workspace를 실제 경로로 정규화한 뒤 이전 결함8개가 모두 재현됐다.

이후 실행 `38355939655`에서 새 파일 읽기가 변경되지 않은 복사본도 거절했다. [Windows 직접 비교 실행 38356080542](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38356080542)에서 `copy2`한 합성 파일의 경로 `lstat`와 descriptor `fstat`는 identity·크기·mtime·mode·attributes가 동일하나 `st_ctime_ns`만 달랐다. ctime은 경로/핸들 사이에 직접 비교하지 않고 각 방식의 전후 값끼리 대조하도록 수정했다. 경로 교체·읽는 중 수정·검증 후 수정 반례와 기존 시험을 유지한 상태에서 위 최신 실행이 통과했다. 초기 실패를 성공 기록으로 덮어쓰지 않는다.

## 원장 갱신과 범위

양쪽 검증 후 prepare job은 AIK-083에만 evidence1개와 todo→blocked 상태 이력1개를 추가했다. 다른89개 태스크·완료 기준311개·의존성365개·스키마의 동일성을 대조하고 SQL/Markdown과 README 집계를 갱신했다. [생성 결과와 소스 해시](ci-validation.json)에 이를 기록했다.

생성된 정본 DB Git blob은 `9b9cf41256e0bc41a651c1b570e6761b46eb1d13`, SHA-256은 `87a7e9210da1f24663633419d3aaf1fbf8c308c799fa74f07448a37606bc3a94`다. 준비 job은 명시한 여섯 파일의 불변 blob만 게시했으며 branch/commit을 바꾸지 않았다. 결과 회수 후 임시 준비 workflow와 Windows stat probe workflow를 모두 삭제했다.

[변경 계약과 한계](../../../docs/acceptance-file-snapshots.ko.md): 검사·계산·digest는 같은 바이트 사본을 사용하고 관찰 가능한 파일 변경을 거절한다. 동일 사용자 공격자에 대한 OS 격리나 다중 파일 원자성을 보증하지 않는다. AIK-003과 실제 VM/AGS 사용자 흐름·설치본·계약 승인은 미완료이므로 AIK-083은 blocked이며 전체 태스크 done 또는 release approval이 아니다.
