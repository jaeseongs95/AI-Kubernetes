# AIK-004 — IPC 수명주기 보강·회귀 근거

대상은 **AIK-004 한 건**이다. 기준 `main`은 `02f25bebbbfc332dc04ad182b2ec827d568b10e6`; 이전 IPC 소스 Git blob은 `9be3d3fad14c70babbafa0bbe1f6ec1d92250c10`이다. 원본 소스와 기존 10개 테스트/preload를 각자의 Git blob과 바이트 대조한 뒤 로컬 시험했다.

## 수정 전 재현

신규 테스트의 첫 8개를 이전 IPC 소스에서 실행해 **0 PASS / 8 FAIL**을 확인했다. 요청 identity의 await 중 변조, backend 응답의 공유 객체 변경, UUID 대소문자 재전송, 미종료 작업의 보관시간 만료, 비동기 무응답, Proxy trap 실행, 오류 코드 반사, inherited toJSON 실행을 각각 분리했다. 재현용 문자열은 모두 synthetic canary다.

## 수정 후

기존 IPC 테스트 10개를 수정하지 않고 신규 25개를 추가했다. snapshot·응답 기한·실제 작업 종료 전 슬롯 보존·late result/rejection·cooperative abort·재전송·시계 오류·범위 변경·Promise/Proxy 경계를 검사한다. 로컬 Node v22.16.0 (Linux)에서 **35/35 PASS, 실패/취소/skip 0**을 실행했다. Node contract 테스트이며 실제 Electron 설치 앱의 실행이라고 주장하지 않는다.

[로컬 결과 및 소스 해시](local-validation.json)와 CI 로그는 각자의 source identity에 결속한다. [SQLite 기록 결과](ci-validation.json)는 원장을 실제 읽고 수정·검증한 실행에서 생성했다. 원장 갱신기는 `record_ledger.py`이며, 작업당 증거 1개와 상태 이력 1개만 추가하고 다른 태스크·완료 기준·의존성·스키마가 동일한지 검증한다. README의 상태 집계와 SQL/Markdown 파생본도 함께 갱신한다.

## 실제 CI와 원장 기록

검증 소스 `864fc3aa17ed5ace15ecf869e34f731b627abf93`의 [실행 38340757298](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38340757298)에서 Windows Server 2022 및 Ubuntu 24.04의 IPC 35개, 기존 원장 회귀 53개와 원장 검증이 통과했다. 이어 같은 소스의 `prepare` job에서 원장을 수정하고, 기존 90개 태스크·311개 완료 기준·365개 의존성 및 다른 태스크 행을 대조했다. 신규 evidence 1개와 `todo → blocked`의 `status_change` 이벤트 1개만 AIK-004에 추가했다. 이 결과의 source hash 네 개는 로컬 시험의 원본과 같다.

원장 준비 job은 명시한 다섯 파일의 Git blob만 생성했고, branch/commit은 수정하지 않았다. 임시 쓰기 권한 workflow는 결과 회수 후 삭제했으며 최종 제품/검증 workflow에는 포함되지 않는다. 정본 DB Git blob은 `611bcdb4eb30d0a1c73687deb5d2080130db704f`, SHA-256은 `bd0ac3f63486d5d27f9f701a54612114df15bd90d33f3bc3fd8e0c313a74f090`이다.

### 추가 검사에서 발견한 테스트 정리 오류

[앞선 실행 38340406010](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38340406010)은 IPC 35개와 실제 원장 검증에는 통과했지만, Windows의 기존 `BacklogPathTests` 9개가 임시 DB 삭제 시 `WinError 32`로 실패했다. 해당 fixture의 `with sqlite3.connect(...)`는 transaction 종료만 수행하고 연결을 닫지 않았다. 테스트 두 위치에 `contextlib.closing`만 적용해 연결을 명시적으로 닫았으며 기존 테스트의 assertion·제품 원장 도구·태스크 정의는 변경하지 않았다. 수정 후 위 실행에서 Windows의 원장 회귀 전체가 통과했다. 처음 실패한 근거를 최신 성공으로 덮어쓰지 않는다.

## 범위와 미완료 조건

AIK-004는 `blocked`: 선행 AIK-083 미완료, 제품 main/sidecar/preload 패키징 및 실제 Windows 11 설치 통합 검증이 남아 있다. 기한 초과가 실제 작업 종료 또는 OS 격리를 뜻하지 않는다. replay 기록은 프로세스 메모리 범위이며 supervisor 교체/영속 인가·VM/AGS 통합은 별도 범위다.
