# AIK-093 실제 관측: prompt cache 적중과 thinking 블록 왕복 (2026-10-14)

- 도구: `tools/probe_prompt_cache.py`
- 경로: egress gateway(자격 주입, `/v1/messages`만 허용) → Messages API
- 요청 모델: `claude-opus-5-5`. 응답 `model` 값도 같다(provider 보고값).
- 보고서: `probe-report.json` (sha256 `e54a3e1482c1f15ea33c1f2c3a328088532b8bddee958044e842d369ff694cf6`)
- 보고서에 들어 있는 것: 토큰 수, 블록 종류, 상태 값, 요청 header 이름
- 보고서에 없는 것: 프롬프트 본문, thinking 본문, signature, 자격 값

## 결과

| 관측 | 값 |
|---|---|
| 캐시 1회차 | cache write 15,614(5분 보존 15,614 / 1시간 0), cache read 0 |
| 캐시 2회차 | cache write 0, cache read 15,614 |
| thinking 1턴 | stop `tool_use`, tool call 1개, opaque `thinking` 1개(위치 0), cache write 16,023 |
| thinking 2턴(블록 재전송) | stop `end_turn`, cache read 16,023, cache write 150 |
| HTTP | 4회 모두 200. redirect·재시도 없음 |

## 범위

- 이번 실행에서 이 경로가 동작했다는 관측일 뿐이다. route qualification(AIK-086)이나 다른 provider에 대한 판정이 아니다.
- **NOT_RUN**: LONG(1시간) 보존 적중, Windows 호스트, thinking을 요청하지 않았을 때의 블록 수신 여부.
