# 사용자 PC Windows 11 실기 실행 (2026-10-14)

사용자가 자신의 Windows 11 PC에서 handoff 패키지를 직접 실행한 결과다. 패키지는 main `e025f41c97753ee464e11ec5e21282018d2c6d40`에서 빌드한 [CI run 38439252870 attempt 2](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38439252870)의 산출물이다.

## 판정

- **AIK-003**: `BLOCKED`(종료 코드 2), 무결성 오류 0. 청정 Windows 11 표준 사용자 판정 근거가 아니다.
- **AIK-022**: `aik022_auth_vault`가 `PASS_SYNTHETIC_SINGLE_USER`다. 합성 실패 경로가 실제 Windows 11 사용자 DPAPI에서 통과했다.

검사기 `tools/review_windows_client.py`를 실행했다. 신뢰 기준 `--expected-manifest-sha256`에는 CI의 `build-handoff.json` 값 `b85ee7ce8d0d4dcdb95fdbc578bf12f8f07f511a61bbf486d253ee1a92f78a66`을 썼다. 제출된 manifest의 hash와 같았다.

## 실제로 관측된 것

**실행 환경**
- Microsoft Windows 11 Pro, 26H2(build 26300), product type 1(client)
- AMD Ryzen 7 250, 논리 프로세서 16개, RAM 약 29.7 GiB
- Windows PowerShell `5.1.26100.9549 Desktop`, 문화권 ko-KR
- GitHub Actions 밖의 로컬 실행
- 기간: 2026-10-14T00:39:07Z ~ 00:39:33Z

**검사 결과**
- handoff 검사 8개가 모두 PASS다: 패키지 hash, BOM 변환, PowerShell 5.1 parse, 설치·GUI·worker·DPAPI·제거, engine·Python DLL 식별, GUI 증거.
- 설치 파일 `AIK-Foundation-Spike-win-x64-dev-unsigned.exe`(sha256 `a29f32bb…b475`)는 `NotSigned`다. 설치, 런타임 번들, 패키지 GUI, 제거가 PASS다.
- 앱 카드 4개가 PASS다: health `SIDECAR_READY`, worker `WORKER_PROBE_OK`, vault `VAULT_ROUNDTRIP_OK`, auth_vault `AUTH_VAULT_FAILURE_PATHS_OK`.
- auth_vault의 10개 case는 모두 기대 코드와 같다. `other_user_profile`은 `NOT_RUN_SINGLE_USER_SESSION`이다.

**화면**
- `screenshot.png`에는 "4 / 4 확인 완료"와 카드 1~3의 결과가 보인다.
- 네 번째 카드는 제목까지만 화면에 들어오고 결과 줄은 캡처 범위 밖이다. 그 결과는 `application-report.json`의 `checks.auth_vault`로 확인했다.

## 청정 판정을 막은 이유 (검사기 blockers)

| 코드 | 실제 관측 |
|---|---|
| `ELEVATED_OR_UNKNOWN_USER`, `NOT_CONFIRMED_STANDARD_USER` | 승격된 관리자 토큰(`elevated: true`, Administrators 구성원) |
| `DEVELOPER_TOOLS_DETECTED` | node, npm, git이 PATH, uninstall registry, 일반 설치 위치 등에서 9건 관측됨 |
| `MISSING_CLEAN_PREPARATION_RECORD` | `client-preparation.json`이 없음. 평소 쓰는 PC여서 사용자가 의도적으로 작성하지 않음 |

## NOT_RUN

다음은 실행하지 않았다.
- 청정 Windows 11 표준 사용자 판정
- 개발 도구 부재
- 게시자 서명
- 제품 연결
- 다른 Windows 사용자 프로필의 DPAPI 거부
- 실계정 인증 경로

화면은 위에 적은 내용까지만 확인했다. 최신 보안 업데이트, hardware, 오프라인 조건은 독립적으로 검토하지 않았다.

## 파일

**첨부 원본**
- `aik-evidence-2f1c944b.zip`: 사용자가 첨부한 원본, sha256 `0c4df26643ee27f403f49700e7a8dc2ec0df8662e6f4d5d882de190b46ab993b`

**검사 결과**
- `review.json`: 검사기 출력

**압축 해제본**
- `2f1c944b554e4031bdfc745bbf7cb3b7/`: 위 ZIP을 바이트 그대로 푼 것
- Windows `\` 경로 구분자만 `/`로 바꿨다.
- 절대 경로와 상위 경로 항목은 없었다.

**개인 정보**
- 실행 경로에 Windows 계정 폴더 이름(`Lenovo`)이 들어 있다.
- SID, 이메일, 인증 정보는 없다.
