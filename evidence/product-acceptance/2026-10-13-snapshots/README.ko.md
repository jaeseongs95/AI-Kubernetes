# AIK-083 — 파일 oracle snapshot 회귀 근거

단일 대상 태스크는 **AIK-083**이다. 기준 main은 `b60f7c71c96dd0d95fa98214d744db04dd65e562`, 이전 검사기 Git blob은 `766a91bf50596377b5a72bcb301d6396a1b78b34`다. 기존 12개 시험·인수계약·고정 프로젝트·지원 matrix는 변경하지 않는다.

`record_evidence.py`는 실제 정본 SQLite의 AIK-083과 완료 기준/선행 관계를 먼저 읽는다. `apply_checker_patch.py`로 고정 이전 source에서 새 검사기를 결정적으로 생성하고, 별도 임시 디렉터리의 이전 검사기에 선택 반례8개를 실행한다. 이후 수정본의 기존12+신규24 테스트를 검증한다. source commit과 생성된 checker의 실제 Git blob/SHA-256을 별도로 기록해, branch의 이전 파일을 이미 검증한 새 파일로 혼동하지 않도록 한다.

두 OS 검증 후 prepare job은 AIK-083에만 evidence1개와 todo→blocked 상태 이력1개를 추가하고 다른89개 태스크·완료 기준311개·의존성365개·스키마의 동일성을 검사한다. SQL/Markdown과 README 상태 집계를 함께 생성한다. 테스트와 생성 파일의 실제 결과는 ci-validation.json에 기록한다.

준비 job은 명시된 여섯 파일의 불변 Git blob만 게시한다. branch/commit 변경은 하지 않으며, 결과 회수 뒤 임시 쓰기 workflow를 삭제한다. 최종 PR은 생성된 checker와 정본 원장을 포함한 그대로 다시 검증한다. 로컬 실행이나 실제 provider/Windows 제품 qualification을 했다고 주장하지 않는다.

AIK-003과 실제 VM/AGS 사용자 흐름·설치본·계약 승인이 남아 있다. 이번 파일 증거 결속 수정을 전체 태스크 done 또는 release approval로 전환하지 않는다.
