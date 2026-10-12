# AIK-005 runtime 저장소 구현·검증 근거

## 대상과 판정

이번 사용자 지시는 원장에서 태스크 하나를 선택해 구현하고 커밋·푸시하는 것이다. 대상은 **AIK-005 — AIK 설정·placement·lease 저장소와 마이그레이션** 하나다. 기존 세 완료 기준, 산출물, 검증 방법 및 AIK-001·AIK-083 의존성을 변경하지 않았다.

`src/aik_runtime/`에 독립 저장 모듈을 구현했다. [API·스키마·복구 계약](../../../docs/runtime-storage.ko.md)에 사용법과 한계를 기록했다. 이 부품의 구현 검증은 통과했지만 **선행 AIK-083이 `todo`이므로 원장 상태는 `blocked`**다. `in_progress`/`done` 조건을 우회하거나 AIK-003의 깨끗한 Windows 11 검증을 대신하지 않는다.

| 원래 완료 기준 | 구현과 검증 근거 |
| --- | --- |
| pool/credential_ref/profile/placement/lease와 VM/AGS 외부 ID 저장, 실비밀·VM 권위 Task 사본 제외 | 고정된 다섯 metadata dataclass와 별도 SQLite schema. 임의 payload·추가 secret 필드·잘못된 locator·변조 객체 거절. 다섯 모델 저장·재열기 검증 |
| 원자적 쓰기·revision 충돌, 손상/마이그레이션 실패 시 원본 보전 | SQL revision CAS, transaction/SAVEPOINT, 실제 hot journal 강제 종료·복구, v1→v2 migration 및 실패 주입, 손상·타 애플리케이션·알 수 없는 버전 거절 |
| 운영 데이터가 개발 원장 또는 Git 추적 경로에 기록되지 않음 | 사용자 데이터 폴더 기본값, Git의 tracked/untracked/ignored 경로·원장 sidecar·링크·예약 파일명 거절. 모든 runtime DB fixture는 저장소 밖 임시 경로 |

## 로컬 실행

- 실행 완료: **2026-10-12 12:52:26 UTC**
- 명령: `python tools/verify_runtime_storage.py --output <새로운 외부 경로>`
- 환경: Linux 6.18.44 x86_64, Python 3.12.14, SQLite 3.53.1
- 결과: **21/21 PASS, failure 0, error 0, skip 0**
- 원본 결과: [local-validation.json](local-validation.json)
- 결과 SHA-256: `3b6e343169389c0fce30b9cf3df27d2901dfe643706e50f74471ca035470b9fc`

결과의 `source_commit`은 구현 전 HEAD인 `325a7f7fa2c7406f49bad3646166a000587d881a`이며 `working_tree_dirty=true`다. 테스트는 이 HEAD에 추가한 구현 파일에서 실행했다. JSON의 `sources`에 모든 runtime 모듈·테스트·검증기·workflow의 SHA-256을 보존했고, 기록 시 파일 bytes와 일치함을 확인했다. 따라서 이 결과를 이전 커밋의 기능으로 주장하지 않는다. 푸시 후 CI 결과는 실제 구현 commit에 별도로 결합한다.

실제 crash 검증은 SQLite cache를 줄이고 512개의 미커밋 쓰기를 수행한다. 정상 hot-journal magic과 DB 페이지의 실제 디스크 변경을 확인한 뒤 writer 프로세스를 강제 종료한다. 재열기 후 기존 다섯 모델과 revision이 보존되고 미커밋 행은 없으며, 이후 새 쓰기와 integrity 검사가 통과했다.

최종 원장 변경 뒤 기존 기능을 포함한 전체 회귀도 **134/134 PASS, failure 0, error 0, skip 0**로 완료했다. `AIK_HANDOFF_TEST_POWERSHELL`에 실제 PowerShell 7.6.6 실행 경로를 지정하고 `python -m unittest discover -s tests -v`를 실행했다. [전체 실행 로그](full-regression.log)에 테스트별 결과와 14.566초 실행 결과를 보존했다. 원장 자체 검증도 `VALID`, ready 0으로 통과했다.

## 독립 검토와 보완

별도 구현 검토에서 두 경로를 보완했다. canonical 경로가 Git 내부를 가리키면 상위 디렉터리를 만들기 전에 거절한다. 또한 hot journal 복구 뒤 migration 게시와 원본 복원이 연속 실패해도 유일한 원본 사본을 지우지 않는다. 두 경우 모두 영구 회귀 테스트에 포함했다.

복구 사본 테스트는 보존된 main DB와 journal이 실패 전 bytes와 정확히 같음을 검사하고, 그 쌍을 별도 경로로 복사해 실제 복구·마이그레이션한 뒤 모든 committed 모델과 revision을 다시 확인한다. 독립 검토에서는 게시 후 디렉터리 동기화 실패도 주입해 새 schema와 옛 journal을 섞지 않고 원본 사본을 유지함을 확인했다.

검증은 synthetic metadata·로컬 파일·프로세스·SQLite 동작에 한정한다. 실제 벤더 호출, GUI/VM/AGS 제품 연결, 대규모 DB 처리량, 전원 차단·저장장치 고장 및 깨끗한 Windows 11 설치 qualification은 이 실행에서 판정하지 않았다.

## CI

전용 `Runtime storage` workflow가 동일한 구현 commit에서 `ubuntu-24.04`와 `windows-2022`를 실행한다. 각 결과는 source commit·파일 SHA-256·실행기 정보·테스트별 결과와 함께 artifact로 남는다. 이 최초 구현 기록 시점에는 아직 푸시 전이므로 CI 결과는 **PENDING**이며, 실제 완료 후 실행 링크와 원본 artifact를 추가한다.
