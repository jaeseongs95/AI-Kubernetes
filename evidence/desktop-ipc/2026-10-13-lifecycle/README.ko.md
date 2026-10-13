# AIK-004 — IPC 수명주기 보강·회귀 근거

대상은 **AIK-004 한 건**이다. 기준 `main`은 `02f25bebbbfc332dc04ad182b2ec827d568b10e6`; 이전 IPC 소스 Git blob은 `9be3d3fad14c70babbafa0bbe1f6ec1d92250c10`이다. 원본 소스와 기존 10개 테스트/preload를 이 blob과 바이트 대조한 뒤 로컬 시험했다.

## 수정 전 재현

신규 테스트의 첫 8개를 이전 IPC 소스에서 실행해 **0 PASS / 8 FAIL**을 확인했다. 요청 identity의 await 중 변조, backend 응답의 공유 객체 변경, UUID 대소문자 재전송, 미종료 작업의 보관시간 만료, 비동기 무응답, Proxy trap 실행, 오류 코드 반사, inherited toJSON 실행을 각각 분리했다. 재현용 문자열은 모두 synthetic canary다.

## 수정 후

기존 10개 테스트를 수정하지 않고 신규 25개를 추가했다. snapshot·응답 기한·실제 작업 종료 전 슬롯 보존·late result/rejection·cooperative abort·재전송·시계 오류·범위 변경·Promise/Proxy 경계를 검사한다. 로컬 Node v22.16.0 (Linux)에서 **35/35 PASS, 실패/취소/skip 0**을 실행했다. Node contract 테스트이며 실제 Electron 설치 앱의 실행이라고 주장하지 않는다.

[로컬 결과 및 소스 해시](local-validation.json)와 CI 로그는 각자의 source identity에 결속한다. [SQLite 기록 결과](ci-validation.json)는 원장을 실제 읽고 수정·검증한 실행에서 생성한다. 원장 갱신기는 `record_ledger.py`이며, 작업당 증거 1개와 상태 이력 1개만 추가하고 다른 태스크·완료 기준·의존성·스키마가 동일한지 검증한다. README의 상태 집계와 SQL/Markdown 파생본도 함께 갱신한다.

## 범위와 미완료 조건

AIK-004는 `blocked`: 선행 AIK-083 미완료, 제품 main/sidecar/preload 패키징 및 실제 Windows 11 설치 통합 검증이 남아 있다. 기한 초과가 실제 작업 종료 또는 OS 격리를 뜻하지 않는다. replay 기록은 프로세스 메모리 범위이며 supervisor 교체/영속 인가·VM/AGS 통합은 별도 범위다.
