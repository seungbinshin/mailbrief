# 사내 LLM 메일 분류 — 구현 가이드와 점검 항목

이 문서는 사내 LLM으로 메일을 자동 분류할 때 일부 메일이 실패하는 원인을 찾고 고치는 절차입니다.

## 1. 처리 흐름과 실패 지점

```
메일 → [A] 전처리 → [B] 프롬프트 → [C] LLM 호출 → [D] JSON 파싱 → [E] 정규화·검증 → 결과
                                         실패 시: 짧은 본문으로 1회 재시도 → 그래도 실패: 규칙 기반 분류
```

| 단계 | 하는 일 | 대표 실패 |
|---|---|---|
| A 전처리 | HTML → 텍스트, 인용·서명 제거, 길이 제한 | 본문 없음, 보호된 메일, 이미지 메일 |
| B 프롬프트 | 분류 기준(config)을 넣어 system prompt 생성 | 분류 기준이 겹침, system role 미지원 |
| C 호출 | OpenAI 호환 `/v1/chat/completions` | 타임아웃, 컨텍스트 초과, 인증 |
| D 파싱 | `<think>`, 코드펜스, 쉼표 오류 정리 후 JSON 파싱 | 설명문 출력, 잘린 JSON |
| E 정규화 | 라벨 별칭, 우선순위 단어, 날짜 형식 보정 | 목록에 없는 라벨, 한국어 키 |

모든 실패는 **코드**로 기록됩니다. `mailbrief diagnose` 명령으로 원인별 건수와 확인할 항목을 봅니다.

## 2. 점검 절차 (순서대로)

1. 연결을 확인합니다.
   `mailbrief check-llm` 을 실행합니다. `parsed: {'ok': True}` 가 나오면 연결은 정상입니다.
2. 실패 기록을 확인합니다.
   `mailbrief diagnose` 를 실행합니다. 가장 많은 실패 코드부터 3절 표를 보고 고칩니다.
3. 최근 메일로 다시 측정합니다.
   `mailbrief diagnose --sample 30` 을 실행합니다. 성공률, 분류 분포, 정규화 경고를 확인합니다.
4. 실패한 메일만 다시 실행합니다.
   `mailbrief diagnose --retry` 를 실행합니다. 프롬프트와 모델 원문 출력이 `out/debug/` 에 저장됩니다.
5. 원문 출력을 직접 읽습니다.
   `=== RAW OUTPUT ===` 부분에서 모델이 실제로 무엇을 출력했는지 확인합니다. 대부분의 원인은 여기에서 보입니다.
6. 설정이나 프롬프트를 고친 후 3번을 다시 실행합니다. 성공률을 비교합니다.

> 프롬프트를 고쳤으면 `src/mailbrief/prompts.py` 의 `PROMPT_VERSION` 을 올립니다. 이전 캐시 결과가 무시되고 다시 분류됩니다.
> `analysis.categories` 를 바꾸면 캐시 키가 자동으로 바뀝니다.

## 3. 실패 코드별 원인과 조치

| 코드 | 증상 | 확인할 것 | 조치 |
|---|---|---|---|
| `E_TRANSPORT` | 타임아웃, 접속 거부, 5xx, 429 | 사내 프록시가 내부 주소를 가로채는지(`NO_PROXY`), 서버 큐 길이 | `timeout_s` 증가, `--workers` 감소(동시 호출 수), `max_retries` 증가 |
| `E_AUTH` | 401/403 | `api_key_env` 의 환경변수 이름과 값 | 키 재발급, 게이트웨이 권한 확인 |
| `E_CONTEXT` | 프롬프트가 너무 김 | 서버 `max_model_len`, 긴 스레드·표가 있는 메일 | `max_body_chars`(기본 4000), `retry_body_chars`(기본 1500) 감소 |
| `E_HTTP` | 기타 400/422 | `model` 이름 오타, 지원하지 않는 파라미터 | `json_mode = false`, `extra_body` 제거 후 재시도 |
| `E_EMPTY` | 빈 응답 | 서버의 안전 필터, stop 토큰, chat template | `system_in_user = true` 시도, 서버 로그 확인 |
| `E_TRUNCATED` | `finish_reason = length`, JSON이 중간에 끊김 | 한국어는 토큰이 많음. 추론 모델은 `<think>` 에 토큰을 씀 | `max_tokens` ≥ 1024, 추론 끄기(`extra_body`) |
| `E_NO_JSON` | "죄송합니다…" 같은 설명문만 출력 | system role 무시 여부, JSON 모드 지원 여부 | `json_mode = true`, `system_in_user = true`, `temperature = 0` |
| `E_BAD_JSON` | 괄호 누락, 작은따옴표, 주석 | 원문 출력 | 서버 측 JSON 강제 디코딩(vLLM `guided_json`/`response_format`) 사용 |
| `E_SCHEMA` | JSON은 정상이나 키가 다름(예: `"요약"`) | 원문 출력의 키 이름 | 프롬프트 예시의 키를 모델이 따르도록 예시 추가, 키 별칭은 코드에서 추가 |

정규화 경고(실패는 아님)도 확인합니다.
- `unknown category 'X' -> other`: 모델이 목록에 없는 라벨을 냅니다. `category_aliases` 에 `X = "<키>"` 를 추가하거나 분류 설명을 고칩니다.
- `bad due '...'`: 날짜 형식이 틀렸습니다. 프롬프트의 날짜 규칙과 예시를 확인합니다.
- `output truncation repaired`: 잘린 출력을 복구해서 사용했습니다. `max_tokens` 를 늘립니다.

## 4. 사내 LLM 설정 점검 항목

| 항목 | 점검 내용 | 설정 |
|---|---|---|
| API 호환성 | `/v1/chat/completions` 가 OpenAI 형식으로 응답하는지 | `base_url` (끝에 `/v1` 포함) |
| system role | Gemma 계열 등은 system role을 지원하지 않거나 무시함 | `system_in_user = true` |
| JSON 강제 | vLLM/TGI는 `response_format` 또는 guided decoding 지원 | `json_mode = true` (400이 나면 자동으로 끄고 재요청) |
| 추론(thinking) 모델 | Qwen3 등은 `<think>` 블록에 토큰을 많이 씀 | `extra_body = { chat_template_kwargs = { enable_thinking = false } }` |
| 출력 길이 | 한국어 요약 + 할 일 + 일정 JSON ≈ 300~700 토큰 | `max_tokens = 1024` 이상 |
| 입력 길이 | 한국어 4000자 ≈ 2000~4000 토큰 + 프롬프트 ≈ 800 토큰 | 컨텍스트 8k 이상 권장 |
| 결정성 | 같은 메일은 같은 결과여야 함 | `temperature = 0` |
| 동시성 | 서버 GPU 수보다 많은 동시 호출은 타임아웃 원인 | `mailbrief run --workers 2` |
| 인증서 | 사내 CA로 서명된 HTTPS | `ca_bundle = "C:\\certs\\corp-ca.pem"` (`verify_tls = false` 는 마지막 수단) |

## 5. 입력 메일 유형별 점검

| 메일 유형 | 문제 | 현재 처리 / 권장 |
|---|---|---|
| 회의 초대(본문 없음) | 본문이 비어서 모델이 설명문을 냄 | 제목·수신자 정보로 분류. `(empty body)` 표시 |
| 보안 등급(MIP/DRM) 보호 메일 | 본문 대신 "보호된 메시지" 문구만 있음 | 규칙 분류로 처리됨. 필요하면 `skip_senders` 또는 제목 규칙 추가 |
| 이미지·첨부만 있는 메일 | 본문이 거의 없음 | 첨부 여부(`ATTACHMENTS: yes`)를 프롬프트에 전달 |
| 긴 회신 스레드 | 인용 부분이 대부분 | 인용 마커(`-----Original Message-----`, `보낸 사람:`, `On … wrote:`)에서 자름. 사내 메일 클라이언트의 마커가 다르면 `preprocess.py` 의 `_QUOTE_MARKERS` 에 추가 |
| 표가 큰 메일(보고서) | 토큰 초과 | `E_CONTEXT` 후 짧은 본문으로 자동 재시도 |
| 자동 발송(NDR, 시스템 알림) | 분류 가치 낮음 | `skip_senders` 에 주소 추가 → LLM 호출 안 함 |
| 영문/한글 혼합 | 요약 언어가 섞임 | 프롬프트에 "summary: Korean" 고정 |

## 6. 분류 기준 설계 규칙

분류 기준은 코드가 아니라 `config.toml` 에 둡니다.

```toml
[analysis.categories]
action_required = "내가 해야 할 일(검토, 회신, 제출, 승인)이 있는 메일"
meeting = "회의 초대, 일정 변경, 회의록"
fyi = "참고용 공유. 내가 할 일 없음"
announcement = "전사/부서 공지, 교육, 제도 안내"
newsletter = "뉴스레터, 광고, 구독 메일"
other = "위 분류에 맞지 않는 메일"

[analysis.category_aliases]
"조치필요" = "action_required"
"회의" = "meeting"
```

규칙:
1. 라벨 키는 영문 snake_case 로 씁니다. 작은 모델은 한국어 라벨을 변형해서 출력하는 경우가 많습니다.
2. 설명은 한 줄로 씁니다. "무엇이 들어가는지"를 구체적인 예로 씁니다.
3. 분류끼리 겹치지 않게 씁니다. 겹치면 우선순위를 설명에 씁니다. 예: "할 일이 있는 회의 초대는 meeting".
4. 분류는 7개 이하로 유지합니다. 많을수록 작은 모델의 정확도가 떨어집니다.
5. 마지막에 `other` 를 둡니다. 모델이 모르는 라벨을 내면 `other` 로 보정됩니다.
6. 모델이 자주 내는 다른 표현은 `category_aliases` 로 연결합니다(`diagnose` 경고에서 확인).

## 7. 정확도 측정 방법

1. 최근 메일 50~100건을 고릅니다. 각 메일에 정답 분류를 사람이 붙입니다.
2. `mailbrief diagnose --sample 100` 결과와 비교합니다.
3. 분류별로 정확도를 계산합니다. 특히 `action_required` 의 누락(놓친 할 일)을 우선 확인합니다.
4. 틀린 메일은 `out/debug/` 덤프에서 원문 출력을 보고, 분류 설명 또는 예시를 고칩니다.
5. 목표: 실패율(규칙 대체) < 2%, `action_required` 재현율 > 90%.

## 8. 자주 발생하는 원인 요약

1. **max_tokens 부족** — JSON이 중간에 끊김(`E_TRUNCATED`). 가장 흔한 원인입니다.
2. **system role 무시** — 모델이 지시를 무시하고 일반 대화로 답함(`E_NO_JSON`).
3. **추론 모델의 `<think>`** — 출력 토큰을 모두 사용함. 추론을 끄거나 `max_tokens` 를 늘립니다.
4. **본문 없는 메일** — 회의 초대, 보호 메일.
5. **라벨 변형** — `조치 필요`, `Action Required` 등. 별칭으로 해결합니다.
6. **사내 프록시** — 내부 LLM 주소가 프록시로 나감(`E_TRANSPORT`). `NO_PROXY` 에 LLM 호스트를 추가합니다.
