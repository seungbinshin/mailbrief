"""Measure the story classification with real mail, and check the mail collection path.

  mailbrief eval export --days 14 -o out/eval.csv   threads + predicted route. Fill gold_route by hand.
  mailbrief eval score out/eval.csv                 precision / recall, confusion table, story share
  mailbrief check-source --days 3                   what the mail source gives (counts, conversation ids, copies)

gold_route values: story | review | none (also: 스토리, 검토, 아님). gold_category is optional.
The export does not change the story tracker state. Open the CSV in Excel (UTF-8 with BOM).
"""
from __future__ import annotations

import collections
import csv
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, expand
from .models import MailInsight, Thread
from .stories import ROUTES, StoryTracker, judge, reason_ko
from .threads import group_threads, remove_copies

COLUMNS = ["thread_key", "latest", "messages", "subject", "senders", "category", "meeting_type", "pred_route",
           "reasons", "linked_story", "story_title", "owner", "end_condition", "confidence", "summary",
           "gold_route", "gold_category", "note"]
_GOLD = {"story": "story", "스토리": "story", "s": "story", "y": "story", "yes": "story", "o": "story",
         "review": "review", "검토": "review", "r": "review", "?": "review",
         "none": "none", "아님": "none", "n": "none", "no": "none", "x": "none", "일반": "none", "공지": "none"}


def predict_rows(cfg: Config, threads: list[Thread], insights: dict[str, MailInsight],
                 tracker: StoryTracker | None, me: str) -> list[dict]:
    tz = ZoneInfo(cfg.analysis.timezone)
    rows = []
    for t in threads:
        ins = insights.get(t.key)
        if ins is None:
            continue
        v = judge(t, ins, cfg.stories, me)
        linked = tracker.linked_story(t) if tracker else None
        s = ins.story
        rows.append({
            "thread_key": t.key, "latest": t.latest.received.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "messages": len(t.mails), "subject": t.subject,
            "senders": ", ".join(sorted({m.sender_name or m.sender for m in t.mails}))[:120],
            "category": ins.category, "meeting_type": ins.meeting_type,
            "pred_route": v.route if ins.by_llm else "pending",
            "reasons": ", ".join(reason_ko(r) for r in v.reasons) or (f"LLM 실패 {ins.fail_code}" if not ins.by_llm else ""),
            "linked_story": linked.sid if linked else "", "story_title": s.title, "owner": s.owner,
            "end_condition": s.end_condition, "confidence": f"{s.confidence:.1f}", "summary": ins.summary,
            "gold_route": "", "gold_category": "", "note": "",
        })
    return rows


def write_csv(rows: list[dict], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return p


def read_csv(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def gold(v: str) -> str:
    return _GOLD.get((v or "").strip().lower(), "")


def score(rows: list[dict]) -> dict:
    lab = [r for r in rows if gold(r.get("gold_route", ""))]
    conf = {g: collections.Counter() for g in ROUTES}
    for r in lab:
        p = r["pred_route"] if r["pred_route"] in ROUTES else "none"   # pending counts as "not a story"
        conf[gold(r["gold_route"])][p] += 1
    n = len(lab)
    gs = sum(conf["story"].values())
    ps = sum(conf[g]["story"] for g in ROUTES)
    tp = conf["story"]["story"]
    cat = [r for r in lab if (r.get("gold_category") or "").strip()]
    return {
        "labeled": n, "total": len(rows), "confusion": {g: dict(c) for g, c in conf.items()},
        "story_precision": tp / ps if ps else None,
        "story_recall": tp / gs if gs else None,
        "story_caught": (conf["story"]["story"] + conf["story"]["review"]) / gs if gs else None,
        "pred_story_share": ps / n if n else None, "gold_story_share": gs / n if n else None,
        "review_load": sum(conf[g]["review"] for g in ROUTES) / n if n else None,
        "false_story": [r for r in lab if r["pred_route"] == "story" and gold(r["gold_route"]) == "none"],
        "missed_story": [r for r in lab if gold(r["gold_route"]) == "story" and r["pred_route"] not in ("story", "review")],
        "category_accuracy": (sum(r["category"] == r["gold_category"].strip() for r in cat) / len(cat)) if cat else None,
        "pending": sum(r["pred_route"] == "pending" for r in rows),
    }


def format_score(s: dict) -> str:
    pct = lambda v: "-" if v is None else f"{v:.0%}"
    L = [f"라벨 {s['labeled']}건 / 전체 {s['total']}건 (LLM 실패로 보류 {s['pending']}건)", "",
         "정답 \\ 예측   story  review  none"]
    for g in ROUTES:
        c = s["confusion"][g]
        L.append(f"{g:12} {c.get('story', 0):6} {c.get('review', 0):7} {c.get('none', 0):5}")
    L += ["",
          f"스토리 정밀도(예측 스토리 중 정답 스토리): {pct(s['story_precision'])}   목표 >= 90%",
          f"스토리 재현율(정답 스토리 중 예측 스토리): {pct(s['story_recall'])}",
          f"스토리 포착률(정답 스토리 중 스토리+검토): {pct(s['story_caught'])}   목표 >= 95%",
          f"스토리 비율: 예측 {pct(s['pred_story_share'])} / 정답 {pct(s['gold_story_share'])}",
          f"검토 대기 비율: {pct(s['review_load'])}   (높으면 리뷰 부담. min_confidence, 분류 설명 점검)"]
    if s["category_accuracy"] is not None:
        L.append(f"분류 정확도(gold_category 입력 건): {pct(s['category_accuracy'])}")
    if s["false_story"]:
        L += ["", f"잘못 스토리가 된 스레드 {len(s['false_story'])}건 (뉴스레터·알림·공지가 있는지 확인):"]
        L += [f"- {r['subject']} [{r['category']}] {r['summary'][:60]}" for r in s["false_story"][:20]]
    if s["missed_story"]:
        L += ["", f"놓친 스토리 {len(s['missed_story'])}건 (검토 대기에도 없음):"]
        L += [f"- {r['subject']} [{r['category']}] {r['summary'][:60]}" for r in s["missed_story"][:20]]
    return "\n".join(L)


def export_eval(cfg: Config, out: str, days: int, llm=None, source=None, store=None, now: datetime | None = None,
                workers: int = 2) -> Path:
    from .llm import OpenAICompatClient
    from .pipeline import analyze_threads, fetch_threads, make_tracker, my_address
    from .sources import make_source
    from .store import Store

    tz = ZoneInfo(cfg.analysis.timezone)
    now = now or datetime.now(tz)
    source = source or make_source(cfg.source, tz)
    store = store or Store(expand(cfg.output.db_path))
    llm = llm or OpenAICompatClient(cfg.llm)
    _, threads = fetch_threads(cfg, source, now - timedelta(days=days))
    tracker = make_tracker(cfg, store)
    insights, _ = analyze_threads(cfg, threads, llm, store, workers, tracker.prompt_stories() if tracker else [])
    return write_csv(predict_rows(cfg, threads, insights, tracker, my_address(cfg)), out)


def check_source(cfg: Config, days: int = 3, source=None, now: datetime | None = None) -> str:
    """Text report: what the configured mail source returns. Use it to confirm the collection path."""
    from .sources import make_source

    tz = ZoneInfo(cfg.analysis.timezone)
    now = now or datetime.now(tz)
    since = now - timedelta(days=days)
    me = cfg.source.my_address or cfg.source.email
    L = [f"소스: {cfg.source.type} · 내 주소: {me or '(없음)'} · 기간: 최근 {days}일"]
    warn = []
    t0 = time.monotonic()
    source = source or make_source(cfg.source, tz)
    inbox = source.fetch_mails(since, cfg.source.max_mails)
    t1 = time.monotonic()
    L.append(f"받은편지함: {len(inbox)}통 ({t1 - t0:.1f}초) · 읽음 {sum(m.is_read for m in inbox)}통")
    sent = []
    if cfg.source.include_sent:
        try:
            sent = source.fetch_sent(since, cfg.source.max_mails) if hasattr(source, "fetch_sent") else []
            L.append(f"보낸편지함: {len(sent)}통 ({time.monotonic() - t1:.1f}초)")
        except Exception as e:
            warn.append(f"보낸편지함을 읽지 못함: {e}. 내 회신이 스레드에 들어가지 않습니다.")
    allm = inbox + sent
    if allm:
        cov = lambda f: sum(bool(getattr(m, f)) for m in allm) / len(allm)
        L.append(f"conversation_id 있음: {cov('conversation_id'):.0%} · Message-ID 있음: {cov('message_id'):.0%}")
        if cov("conversation_id") < 0.9:
            warn.append("conversation_id가 없는 메일이 많음: 제목+참여자로 스레드를 묶습니다(정확도 낮음).")
    if cfg.source.type == "com" and len(inbox) >= cfg.source.max_mails:
        warn.append(f"max_mails({cfg.source.max_mails})에 도달: 기간 안의 메일 일부가 빠졌을 수 있습니다.")
    if not me:
        warn.append("source.my_address가 비어 있음: 내 메일과 내 회신을 구별하지 못합니다.")
    copies = len(allm) - len(remove_copies(allm))
    threads = group_threads(allm, me, cfg.analysis.skip_senders, cfg.analysis.thread_gap_days)
    multi = [t for t in threads if len(t.mails) > 1]
    L.append(f"사본 제거: {copies}통 · 스레드: {len(threads)}개 (2통 이상 {len(multi)}개) · "
             f"LLM 호출 수(예상): {len(threads)}회 (메일 단위였으면 {len(inbox)}회)")
    pats = [re.compile(p, re.I) for p in cfg.stories.auto_sender_patterns]
    auto = [m for m in inbox if any(p.search(m.sender.lower().split("@")[0]) for p in pats)]
    skip = {s.lower() for s in cfg.analysis.skip_senders}
    L.append(f"자동 발신 메일: {len(auto)}통 (스토리 제외) · skip_senders 일치: {sum(m.sender.lower() in skip for m in inbox)}통")
    for t in sorted(multi, key=lambda t: -len(t.mails))[:5]:
        L.append(f"  - {len(t.mails)}통: {t.subject[:60]}")
    top = collections.Counter(m.sender.lower() for m in auto).most_common(5)
    if top:
        L.append("자동 발신 상위: " + ", ".join(f"{a} {n}통" for a, n in top))
    if warn:
        L += ["", "확인 필요:"] + [f"- {w}" for w in warn]
    return "\n".join(L)
