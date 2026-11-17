# 스토리 추적 — 설계, 판정 규칙, 점검 절차

이 문서는 mailbrief 의 스토리 추적 기능을 설명합니다.
사내 Story-tracker 의 기능을 mailbrief 로 옮겼습니다. 사내 실측에서 찾은 문제를 고쳤습니다.
사내에서 다시 구현할 때 이 문서를 기준으로 사용합니다.

## 1. 구조

| 부분 | 하는 일 | 모듈 |
|---|---|---|
| 읽는 쪽 (수집, 추출, 판정) | 메일을 읽고 스레드로 묶습니다. LLM 으로 사실을 추출합니다. 규칙으로 판정합니다. | `sources/`, `threads.py`, `extract.py`, `stories.judge` |
| 관리하는 쪽 (스키마, 리뷰, 화면) | 스토리를 저장합니다. 검토 대기를 관리합니다. 대시보드와 폴더를 만듭니다. | `stories.StoryTracker`, `story_cli.py`, `dashboard.py` |

Story-tracker 에는 메일을 읽는 앞쪽이 없었습니다. 두 부분을 한 저장소에 둡니다. 그래서 판정 기준과 저장 형식이 같이 바뀝니다.

```
Outlook(받은편지함 + 보낸편지함)
   │  사본 제거(Message-ID), 스레드 묶기(conversation_id → 제목+참여자)
   ▼
스레드 1개 = LLM 호출 1회 (새 회신이 오면 새 리비전 → 1회 더)
   │  요약, 분류, 할 일, 일정, 회의 종류, 스토리 사실(지속성, 담당, 종결 조건, 확신도, 매칭 후보)
   ▼
규칙 판정 (stories.judge)  ──→  story  : 연결된 스토리에 추가, 또는 새 스토리
                           ──→  review : 검토 대기 (사람이 결정)
                           ──→  none   : 스토리 아님 (할 일·일정은 브리핑에 그대로 표시)
                           ──→  pending: LLM 실패. 판정하지 않음. 다음 실행에서 다시 시도
   ▼
SQLite(stories, story_links, story_updates, reviews) → out/stories.html, 스토리 폴더, 브리핑 카드
```

## 2. 스토리 정의

스토리는 계속 추적해야 하는 업무 단위입니다. 메일 1통이 아닙니다. 목표 1개입니다.
다음 3가지 조건이 **모두** 있어야 스토리입니다.

| 조건 | 뜻 | LLM 출력 키 |
|---|---|---|
| 지속성 | 메일 이후에 후속 행동이 있습니다. 여러 단계, 다른 사람의 회신 대기, 여러 날. | `story.persistence` |
| 소유 | 담당자 또는 담당 팀(R&R)이 메일에 있습니다. | `story.owner` |
| 종결 조건 | 언제 끝나는지 문장 1개로 쓸 수 있습니다. | `story.end_condition` |

한 번에 끝나는 일(회신 1회, 교육 1회 이수, 양식 1회 제출)은 스토리가 아닙니다. 이 일은 할 일(action item)로 추적합니다.
뉴스레터, 자동 알림, 공지, 교육·행사 안내는 스토리가 아닙니다.

## 3. 판정 규칙 (결정 표)

LLM 은 사실만 냅니다. 판정은 코드의 규칙이 합니다(`stories.judge`). 결과를 예측하고 테스트할 수 있습니다.

| 순서 | 조건 | 결과 | 검토 사유 |
|---|---|---|---|
| 1 | LLM 실패(규칙 대체 결과) | pending. 판정하지 않음. 캐시하지 않으므로 다음 실행에서 다시 시도 | — |
| 2 | 이미 스토리에 연결된 스레드 | 그 스토리에 업데이트 추가 | — |
| 3 | LLM 이 열린 스토리 S 와 같은 목표라고 함(`story.match`), 공지·자동 메일 아님 | S 에 업데이트 추가 | — |
| 4 | 지속성 없음 | none | — |
| 5 | 지속성 있음 + 공지/뉴스레터 분류 또는 자동 발신 | review | `category_conflict` |
| 6 | 지속성 있음 + 담당 없음 | review | `owner_unknown` |
| 7 | 지속성 있음 + 종결 조건 없음 | review | `end_unknown` |
| 8 | 지속성 있음 + 확신도 < `min_confidence`(기본 0.8 = high 만 통과) | review | `low_confidence` |
| 9 | 3가지 조건 모두 있음, 사유 없음 | story (새 스토리) | 비슷한 스토리가 있으면 `possible_duplicate` |

중요한 규칙:
- **매칭 실패는 승격 사유가 아닙니다.** 열린 스토리와 맞지 않는 메일은 스토리가 되지 않습니다. 조건 3가지를 모두 채울 때만 새 스토리가 됩니다.
- **애매하면 스토리가 아닙니다.** 조건이 하나라도 없거나 확신이 낮으면 검토 대기로 갑니다.
- **공지 안의 실제 업무는 조용히 사라지지 않습니다.** 공지 분류에 지속성이 있으면 검토 대기로 갑니다(5번).
- 사람이 "스토리 아님"으로 처리한 스레드는 다시 올라오지 않습니다. 단, 나중 메일에서 3가지 조건이 모두 분명해지면 검토 대기에 다시 올립니다(`reopened`). 자동으로 스토리를 만들지 않습니다.
- 종결 조건 충족 신호(`story.done`)가 오면 자동으로 닫지 않습니다. `end_reached` 사유로 검토 대기에 올립니다. 사람이 `story close` 로 닫습니다.

## 4. 사내 실측 문제와 조치

| 실측 문제 | 원인 | 조치 | 테스트 |
|---|---|---|---|
| 매칭 안 된 메일을 전부 스토리로 승격 (131건 중 79건) | 기본값이 "스토리" | 기본값을 "스토리 아님"으로 바꿈. 3조건 + 확신도 필요. 실행마다 판정 비율을 표시 | `test_unmatched_mails_are_not_promoted` |
| 뉴스레터, 자동 알림, 교육 안내가 스토리가 됨 | 키워드 판정 | LLM 이 3조건을 추출하고 규칙이 판정. 분류(`non_story_categories`)와 발신자 패턴(`auto_sender_patterns`)으로 차단 | `test_judge_table`, `test_automatic_sender_is_noise` |
| 실제 업무가 공지 폴더로 조용히 흡수 | 공지 분류가 우선 | 공지 + 지속성 → 검토 대기(`category_conflict`) | `test_unmatched_mails_are_not_promoted` |
| 같은 날 두 번째 초안이 병합 때 삭제 | 날짜 단위 키 | 업데이트는 추가만 합니다. 키 = (스토리, 스레드 리비전 + 요약 해시). 정확히 같은 내용만 건너뜁니다 | `test_same_day_second_update_is_kept_and_afternoon_is_latest` |
| 오전 캡처 후 오후 초안이 "더 새롭지 않음"으로 삭제 | 날짜만 비교 | 원본 메일 시각 + 캡처 시각(초·마이크로초까지, UTC)으로 정렬. 삭제하지 않음 | 위와 같음, `test_merge_keeps_all_updates_and_clears_duplicate_flag` |
| 담당자 확정 후에도 리뷰 대기가 풀리지 않음 | 리뷰 상태를 플래그로 저장 | 검토 사유를 매번 계산(담당·종결 조건이 비었는지). 값을 넣으면 바로 풀림. 열린 검토 항목은 나중 메일로 조건이 채워지면 자동으로 스토리가 됨 | `test_review_clears_when_owner_and_end_are_set`, `test_open_review_is_promoted_when_a_later_mail_makes_it_clear` |
| 폴더명에 슬래시 포함 | 제목을 그대로 사용 | `safe_name`: `\ / : * ? " < > |`, 제어 문자, 끝의 점·공백, Windows 예약어(CON, NUL …), `..` 를 처리. 폴더명 앞에 ID(`S3`)를 붙여 중복 없음 | `test_safe_name` |
| 회의 종류를 제목으로만 판정 | 제목 규칙 | LLM 이 본문, 초대 여부(MEETING REQUEST), 참석자, 안건으로 판정(`meeting_type`). 종류는 설정에서 바꿈 | 프롬프트 규칙 |
| RE: 메일이 각각 다른 메일로 판정 | 메일 1통 단위 처리 | 스레드 단위 처리. conversation_id → 제목(RE:/FW:/회신:/전달: 제거) + 공통 참여자 + 14일 이내 | `test_re_chain_is_one_thread_and_one_llm_call`, `test_normalize_subject` |
| 메일 1통마다 LLM 호출 | 메일 단위 | 스레드 리비전 1개 = 호출 1회. 새 메일이 없으면 캐시 사용(호출 0회) | 위와 같음 |

## 5. 검토 대기 처리

`out/stories.html` 의 "검토 대기" 열 또는 `mailbrief story` 로 봅니다.

| 사유 코드 | 표시 | 처리 |
|---|---|---|
| `owner_unknown` | 담당자 미확인 | `mailbrief story promote R2 --owner "김책임" --end "..."` 또는 스토리라면 `story set S3 --owner ...` |
| `end_unknown` | 종결 조건 미확인 | `--end "고객이 최종 보고서를 승인하면 종료"` |
| `low_confidence` | 판정 확신 낮음 | 내용을 읽고 `promote` 또는 `dismiss` |
| `category_conflict` | 공지/뉴스레터 분류지만 후속 업무 있음 | 실제 업무면 `promote`, 아니면 `dismiss` |
| `reopened` | 검토 제외 후 다시 후보로 판정 | `promote` 또는 다시 `dismiss` |
| `possible_duplicate:S3` | 기존 스토리와 중복 가능 | 같은 목표면 `story merge S5 S3`, 다르면 `story confirm S5` |
| `end_reached` | 종결 조건 충족 가능 | 끝났으면 `story close S3`, 아니면 `story confirm S3` |
| `new_activity` | 종료된 스토리에 새 메일 | 다시 진행이면 `story reopen S3`, 아니면 `story confirm S3` |

기존 스토리에 붙이려면 `mailbrief story promote R2 --into S3` 를 사용합니다.
잘못 붙은 스레드는 `mailbrief story show S3` 로 스레드 키를 확인하고 `mailbrief story unlink S3 --key c:...` 로 뺍니다.

## 6. 업데이트(초안) 규칙

1. 스레드에 새 메일이 오면 새 리비전이 됩니다. 연결된 스토리에 업데이트 1건을 추가합니다.
2. 업데이트는 삭제하지 않습니다. 같은 날 여러 건을 모두 보관합니다.
3. 시각은 UTC ISO 8601 로 저장합니다. 원본 메일 시각은 초까지, 캡처 시각은 마이크로초까지 저장합니다.
4. 최신 업데이트 = 원본 메일 시각이 가장 늦은 것. 같으면 캡처 시각, 그다음 ID 순서입니다.
5. 같은 리비전을 다시 처리하면(재실행) 내용 해시가 같으므로 추가하지 않습니다.
6. 스토리 병합(`merge`)은 업데이트와 스레드 연결을 옮깁니다. 정확한 사본만 제거합니다. 병합된 스토리는 `merged` 상태로 남습니다.

## 7. 폴더 구성

`[stories] export_dir = "out/stories"` 로 켭니다. `mailbrief run`, `mailbrief story ...`, `mailbrief dashboard` 실행 후에 다시 씁니다.

```
out/stories/
  README.md                         목록 (생성 파일)
  active/S3 Q4 MPE 검증/STORY.md     진행 중 스토리. STORY.md 와 .mailbrief 는 생성 파일
  done/S1 .../                      완료 스토리
  merged/S4 .../                    병합된 스토리
  review/R2 협력사 샘플 문의.md       검토 대기 (항목을 처리하면 파일을 지움)
```

- 상태가 바뀌면 폴더를 **옮깁니다**. 지우지 않습니다. 스토리 폴더에 넣은 사용자 파일(메모, 첨부)은 그대로 있습니다.
- `review/` 에서는 생성 표시(`<!-- mailbrief:generated -->`)가 있는 파일만 지웁니다.
- 공지, 뉴스레터, 참고 메일은 폴더로 내보내지 않습니다. 이것들은 메일이고 스토리가 아닙니다.

기존 Story-tracker 폴더에서 옮기는 절차:
1. 기존 폴더를 보관용으로 이름을 바꿉니다(예: `stories_old/`).
2. 실제 스토리만 고릅니다. 2절의 3가지 조건으로 확인합니다.
3. 고른 스토리마다 `mailbrief story new --title "..." --owner "..." --end "..." --goal "..."` 를 실행합니다.
4. `mailbrief run` 을 실행합니다. 새 메일 스레드는 LLM 매칭으로 등록한 스토리에 붙습니다.
5. `out/stories/` 를 확인합니다. 필요한 사용자 파일을 새 스토리 폴더로 복사합니다.

## 8. 남은 일 — 사내에서만 할 수 있는 점검

### 8.1 메일 수집 경로 확정 (Outlook)

```powershell
.venv\Scripts\mailbrief check-source --days 3
```

확인 항목:
1. 받은편지함과 보낸편지함 메일 수가 Outlook 화면과 같습니다.
2. `conversation_id 있음` 이 90% 이상입니다. 낮으면 제목+참여자로 묶습니다(정확도 낮음).
3. `Message-ID 있음` 이 높습니다. 낮으면 사본 제거가 발신자+제목+시각으로 동작합니다.
4. `max_mails 에 도달` 경고가 없습니다. 경고가 있으면 `source.max_mails` 를 올립니다.
5. `LLM 호출 수(예상)` 가 메일 수보다 적습니다. 스레드 묶기가 동작합니다.
6. 자동 발신 상위 목록을 봅니다. 빠진 시스템 발신자는 `stories.auto_sender_patterns` 또는 `analysis.skip_senders` 에 추가합니다.

보안 정책으로 보낸편지함을 못 읽으면 `source.include_sent = false` 로 둡니다. 이때 내 회신이 스레드에 들어가지 않습니다.

### 8.2 실제 메일로 분류 정확도 측정

```powershell
.venv\Scripts\mailbrief eval export --days 14 out\eval.csv     # 스레드 목록 + 예측 판정
# Excel 로 out\eval.csv 를 엽니다. gold_route 열에 story / review / none (또는 스토리 / 검토 / 아님)을 씁니다.
# gold_category 열은 선택입니다. 분류 정확도를 같이 봅니다.
.venv\Scripts\mailbrief eval score out\eval.csv
```

측정 지표와 목표:

| 지표 | 뜻 | 목표 |
|---|---|---|
| 스토리 정밀도 | 예측 스토리 중 정답 스토리 비율. 79/131 문제의 지표 | ≥ 90% |
| 스토리 포착률 | 정답 스토리 중 스토리 또는 검토 대기로 간 비율. "조용히 흡수" 문제의 지표 | ≥ 95% |
| 스토리 비율 | 예측과 정답의 스토리 비율 비교 | 차이 작음 |
| 검토 대기 비율 | 사람이 볼 양 | 팀이 정함(예: ≤ 15%) |

틀린 건 처리 순서:
1. "잘못 스토리가 된 스레드" 목록을 봅니다. 뉴스레터·알림이면 `auto_sender_patterns`, `non_story_categories`, 분류 설명을 고칩니다.
2. "놓친 스토리" 목록을 봅니다. `mailbrief diagnose --retry` 의 원문 출력으로 `story` 값을 확인합니다.
3. 검토 대기 비율이 높으면 `min_confidence` 와 프롬프트 예시를 점검합니다. 확신도 낮음이 대부분이면 모델 출력 문제입니다.
4. 프롬프트를 바꾸면 `prompts.py` 의 `PROMPT_VERSION` 을 올립니다. 다시 `eval export` 를 실행합니다.

`mailbrief diagnose --sample 30` 은 최근 30개 스레드의 분류와 스토리 판정을 바로 보여 줍니다.

### 8.3 폴더 재구성

7절의 구성과 절차를 따릅니다.

## 9. 사내 LLM 연결 (오픈코덱스 프록시)

스토리 판정은 사내 LLM 으로 합니다. 키워드로 판정하지 않습니다.
OpenAI 호환 `/v1/chat/completions` 를 쓰므로 프록시 주소를 `[llm] base_url` 에 넣습니다. 끝에 `/v1` 을 포함합니다.

```toml
[llm]
base_url = "https://<사내 프록시 주소>/v1"
model = "<프록시에 등록된 모델 이름>"
api_key_env = "MAILBRIEF_LLM_API_KEY"
max_tokens = 1536        # 스토리 키가 추가되어 출력이 길어짐. 1024 에서 E_TRUNCATED 가 나오면 올림
```

`mailbrief check-llm` 으로 연결을 확인합니다. 실패하면 `docs/CLASSIFICATION_GUIDE.md` 의 실패 코드 표를 봅니다.

## 10. 저장 스키마 (SQLite)

| 테이블 | 키 | 내용 |
|---|---|---|
| `insights` | (`thr:<리비전>`, 모델\|프롬프트 버전\|분류\|회의 종류) | 스레드 분석 캐시 |
| `stories` | `id` (표시 `S<id>`) | 제목, 목표, 담당, 종결 조건, 상태(active/done/merged), 플래그, 병합 대상, 생성·활동 시각 |
| `story_links` | 스레드 키(`c:<conversation id>` 또는 `s:<제목 해시>`) | 스토리 ID, 연결 방법(new/match/promote/thread), 마지막 활동 |
| `story_updates` | `id`, UNIQUE(스토리, 내용 해시) | 스레드 키, 리비전, 메일 ID, 제목, 요약, 원본 시각, 캡처 시각 |
| `reviews` | `id` (표시 `R<id>`), UNIQUE(스레드 키) | 스레드 키 목록, 리비전, 요약, 분류, 사유, 제안(제목·목표·담당·종결 조건), 상태(open/promoted/dismissed) |

검토 사유 중 `owner_unknown`, `end_unknown` 은 저장하지 않습니다. 매번 계산합니다.
제목만으로 만든 연결(`s:`)은 약합니다. 마지막 활동 후 `link_gap_days`(기본 14일)가 지나면 자동으로 붙지 않습니다.

## 11. 설정

| 설정 | 기본값 | 뜻 |
|---|---|---|
| `source.include_sent` | true | 보낸편지함도 읽음 |
| `analysis.max_thread_messages` | 8 | LLM 에 보내는 스레드 메일 수(첫 메일 + 최근 7통) |
| `analysis.thread_gap_days` | 14 | conversation_id 없이 제목으로 묶을 때 최대 간격 |
| `analysis.meeting_types` | regular/review/decision/external/other | 회의 종류와 설명 |
| `stories.enabled` | true | 스토리 추적 켜기 |
| `stories.min_confidence` | 0.8 | 이보다 낮으면 검토 대기 |
| `stories.non_story_categories` | newsletter, announcement | 스토리가 될 수 없는 분류 |
| `stories.auto_sender_patterns` | noreply, notification … | 자동 발신자(정규식, @ 앞부분) |
| `stories.prompt_stories` | 30 | 매칭을 위해 LLM 에 보여 주는 열린 스토리 수 |
| `stories.duplicate_similarity` | 0.6 | 새 스토리와 기존 스토리의 제목 유사도 기준 |
| `stories.link_gap_days` | 14 | 제목 연결의 유효 기간 |
| `stories.export_dir` | "" | 스토리 폴더 경로(빈 값 = 끔) |
| `output.dashboard` | true | `out/stories.html` 생성 |
