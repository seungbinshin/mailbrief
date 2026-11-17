"""`mailbrief diagnose`: find out why some mails fail classification.

Modes:
  diagnose              summary of logged failures, grouped by code, with the fix to check
  diagnose --sample 30  classify the last 30 threads now (no cache): distribution, story route, warnings, failures
  diagnose --retry      re-run only the threads that failed before, dump prompt + raw output to out/debug/
"""
from __future__ import annotations

import collections
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, expand
from .llm import OpenAICompatClient
from .pipeline import fetch_threads, make_extractor, make_tracker, my_address
from .sources import make_source
from .store import Store
from .stories import judge

HINTS = {
    "E_TRANSPORT": "엔드포인트 접속 실패/타임아웃/5xx/429. base_url, 프록시(사내 NO_PROXY), timeout_s, 동시 호출 수(--workers) 확인.",
    "E_AUTH": "401/403. API 키 환경변수(api_key_env)와 키 권한 확인.",
    "E_CONTEXT": "프롬프트가 모델 컨텍스트보다 김. max_body_chars / retry_body_chars 줄이기, 서버 max_model_len 확인.",
    "E_HTTP": "기타 4xx. model 이름, 요청 파라미터(response_format 지원 여부) 확인. json_mode=false 시도.",
    "E_EMPTY": "빈 응답. 모델이 안전 필터/중단 토큰으로 끝났는지, chat template 적용 여부 확인.",
    "E_TRUNCATED": "max_tokens 부족으로 JSON이 잘림. llm.max_tokens 늘리기(>=1024), summary 길이 규칙 확인.",
    "E_NO_JSON": "JSON 대신 설명문 출력. json_mode=true(서버 guided decoding) 사용, system prompt 지원 여부 확인, temperature=0.",
    "E_BAD_JSON": "JSON 문법 오류. json_mode=true 또는 서버 측 guided_json 사용. 주석/작은따옴표/누락된 괄호를 raw에서 확인.",
    "E_SCHEMA": "키 이름이 다름(예: 한국어 키). 프롬프트 예시와 키 이름을 맞추거나 출력 예시를 모델에 맞게 수정.",
}


def _print_failures(store: Store, limit: int) -> None:
    rows = store.failures(limit)
    if not rows:
        print("기록된 실패 없음.")
        return
    by = collections.Counter(r["code"] for r in rows)
    print(f"최근 실패 {len(rows)}건\n")
    for code, n in by.most_common():
        print(f"[{code}] {n}건 — {HINTS.get(code, '')}")
    print("\n최근 항목:")
    for r in rows[:15]:
        raw = (r["raw"] or "").replace("\n", " ")[:120]
        print(f"- {r['created'][:16]} [{r['code']}] {r['subject'][:50]!r} body={r['body_chars']}자\n"
              f"    msg: {r['message'][:140]}\n    raw: {raw}")


def diagnose(cfg: Config, sample: int = 0, retry: bool = False, limit: int = 200, days: int = 7) -> None:
    store = Store(expand(cfg.output.db_path))
    if not sample and not retry:
        _print_failures(store, limit)
        return

    tz = ZoneInfo(cfg.analysis.timezone)
    source = make_source(cfg.source, tz)
    cfg.source.max_mails = max(sample, cfg.source.max_mails)
    _, threads = fetch_threads(cfg, source, datetime.now(tz) - timedelta(days=days))
    if retry:
        failed = {r["email_id"] for r in store.failures(limit)}
        threads = [t for t in threads if failed & set(t.ids)]
        print(f"재시도 대상 {len(threads)}개 스레드 (기간 {days}일 내)")
    else:
        threads = threads[:sample]
    cfg.analysis.debug_dump = True
    ex = make_extractor(cfg, OpenAICompatClient(cfg.llm), None)
    tracker = make_tracker(cfg, store)
    open_stories = tracker.prompt_stories() if tracker else []
    cats, routes = collections.Counter(), collections.Counter()
    for t in threads:
        ins = ex.analyze(t, open_stories)
        cats[ins.category if ins.by_llm else f"(fallback) {ins.category}"] += 1
        route = judge(t, ins, cfg.stories, my_address(cfg)).route if ins.by_llm else "pending"
        routes[route] += 1
        mark = "OK " if ins.by_llm else f"ERR {ins.fail_code}"
        n = f" ({len(t.mails)}통)" if len(t.mails) > 1 else ""
        print(f"{mark:14} {ins.category:16} {route:7} p{ins.priority} {t.subject[:56]}{n}")
        for w in ex.warnings.get(t.latest.id, []):
            print(f"{'':14} warn: {w}")
    for f in ex.failures:
        store.add_failure(f)
    n = len(threads) or 1
    print(f"\n성공률: {(n - len(ex.failures)) / n:.0%} ({n - len(ex.failures)}/{len(threads)} 스레드), "
          f"정규화 경고 스레드: {len(ex.warnings)}")
    print("분류 분포:", json.dumps(dict(cats), ensure_ascii=False))
    print("스토리 판정:", json.dumps(dict(routes), ensure_ascii=False), "(story = 스토리, review = 검토 대기, none = 아님)")
    if ex.failures:
        print(f"실패 원인: {dict(collections.Counter(f.code for f in ex.failures))}")
        print(f"프롬프트/원문 출력 덤프: {Path(cfg.output.dir) / 'debug'}")
        for code in sorted({f.code for f in ex.failures}):
            print(f"  [{code}] {HINTS.get(code, '')}")
