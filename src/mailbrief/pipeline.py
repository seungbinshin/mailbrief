"""End-to-end run: fetch -> threads -> extract (LLM, 1 call per thread) -> stories (rules) -> analyze (rules)
-> brief (LLM) -> render -> deliver.

Read and unread mails are both processed: the source does not filter on the read flag.
Sent items are read too (source.include_sent): my replies complete the threads.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .analyze import Report, briefing_facts, build_report, rule_briefing
from .config import Config, expand
from .extract import Extractor
from .llm import LLMError, OpenAICompatClient
from .models import Email, MailInsight, Thread
from .prompts import BRIEF_SYSTEM
from .cards import build_cards, save_cards, to_cards_html
from .report import to_html, to_markdown
from .sources import MailSource, make_source
from .store import Store
from .stories import IngestResult, StoryTracker, reason_ko
from .threads import group_threads

log = logging.getLogger(__name__)


def my_address(cfg: Config) -> str:
    return cfg.source.my_address or cfg.source.email


def make_extractor(cfg: Config, llm, store: Store | None) -> Extractor:
    a = cfg.analysis
    debug = Path(cfg.output.dir) / "debug" if a.debug_dump else None
    return Extractor(llm, store, cfg.llm.model, my_address(cfg), a.timezone, a.max_body_chars, a.categories,
                     a.category_aliases, a.retry_body_chars, a.min_body_chars, debug,
                     meeting_types=a.meeting_types, max_messages=a.max_thread_messages)


def make_tracker(cfg: Config, store: Store) -> StoryTracker | None:
    return StoryTracker(store, cfg.stories, my_address(cfg)) if cfg.stories.enabled else None


def fetch_threads(cfg: Config, source, since: datetime) -> tuple[list[Email], list[Thread]]:
    """Inbox (+ sent items) since `since`, grouped into threads. Copies are removed."""
    mails = source.fetch_mails(since, cfg.source.max_mails)
    sent: list[Email] = []
    if cfg.source.include_sent and hasattr(source, "fetch_sent"):
        try:
            sent = source.fetch_sent(since, cfg.source.max_mails)
        except Exception as e:  # sent items are optional: threads still work without them
            log.warning("sent items not read (%s): my replies are not in the threads", e)
    threads = group_threads(mails + sent, my_address(cfg), cfg.analysis.skip_senders, cfg.analysis.thread_gap_days)
    return mails + sent, threads


def analyze_threads(cfg: Config, threads: list[Thread], llm, store: Store, workers: int = 4,
                    open_stories: list[tuple[str, str, str]] = ()) -> tuple[dict[str, MailInsight], Extractor]:
    """One LLM call per thread revision. Uses the cache; LLM calls run in parallel threads."""
    ex = make_extractor(cfg, llm, None)  # no store inside threads
    insights: dict[str, MailInsight] = {}
    todo = []
    for t in threads:
        c = store.get_insight(ex.store_key(t), ex.cache_key)
        if c:
            c.thread_key, c.email_ids = t.key, t.ids
            insights[t.key] = c
        else:
            todo.append(t)
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for t, ins in zip(todo, pool.map(lambda t: ex.analyze(t, open_stories), todo)):
                insights[t.key] = ins
                if ins.by_llm and llm is not None:
                    store.put_insight(ins, ex.cache_key, ex.store_key(t))
    for f in ex.failures:
        store.add_failure(f)
    return insights, ex


def story_summary(tr: StoryTracker | None, ing: IngestResult | None) -> dict:
    if tr is None or ing is None:
        return {}
    return {
        "created": [(st.sid, st.title, t.subject) for st, t in ing.created],
        "updated": [(st.sid, st.title, t.subject) for st, t in ing.attached],
        "review_new": [(i.rid, i.subject, ", ".join(reason_ko(r) for r in i.reasons)) for i, t in ing.reviews],
        "review_open": len(tr.open_reviews()) + len(tr.needs_review()),
        "active": len(tr.stories(("active",))),
        "routes": ing.stats(),
    }


def write_story_views(cfg: Config, tr: StoryTracker, now: datetime, run_stats: dict | None = None) -> dict:
    from .dashboard import export_folders, to_dashboard_html
    tz = ZoneInfo(cfg.analysis.timezone)
    out: dict = {}
    if cfg.output.dashboard:
        p = Path(cfg.output.dir) / "stories.html"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(to_dashboard_html(tr, tz, now, run_stats), encoding="utf-8")
        out["dashboard"] = str(p.resolve())
    if cfg.stories.export_dir:
        out["story_folders"] = str(export_folders(tr, expand(cfg.stories.export_dir), tz).resolve())
    return out


def run(cfg: Config, now: datetime | None = None, source: MailSource | None = None,
        llm=None, store: Store | None = None, use_llm: bool = True,
        deliver: bool = True, workers: int = 4) -> Report:
    tz = ZoneInfo(cfg.analysis.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    started = datetime.now(tz)
    source = source or make_source(cfg.source, tz)
    store = store or Store(expand(cfg.output.db_path))
    llm = (llm or OpenAICompatClient(cfg.llm)) if use_llm else None

    a = cfg.analysis
    mails, threads = fetch_threads(cfg, source, now - timedelta(days=a.lookback_days))
    events = source.fetch_events(now - timedelta(days=a.lookback_days), now + timedelta(days=a.lookahead_days))
    log.info("fetched %d mails (%d already read) -> %d threads, %d events",
             len(mails), sum(m.is_read for m in mails), len(threads), len(events))

    tracker = make_tracker(cfg, store)
    insights, ex = analyze_threads(cfg, threads, llm, store, workers, tracker.prompt_stories() if tracker else [])
    ing = tracker.ingest(threads, insights, now) if tracker else None
    me = my_address(cfg)
    rep = build_report(now, threads, insights, events, a, me, store.done_keys())
    rep.stories = story_summary(tracker, ing)
    rep.briefing = rule_briefing(rep)
    if llm is not None:
        facts = briefing_facts(rep)
        if facts:
            try:
                ex.llm_calls += 1
                text = llm.chat(BRIEF_SYSTEM, json.dumps(facts, ensure_ascii=False), json_output=False)
                lines = [l.strip() for l in text.splitlines() if l.strip().startswith("-")][:3]
                if lines:
                    rep.briefing = "\n".join(lines)
            except LLMError as e:
                log.warning("briefing failed, use rule briefing: %s", e)
    rep.stats.update(llm_calls=ex.llm_calls, llm_errors=len(ex.failures),
                     fail_codes=sorted({f.code for f in ex.failures}), norm_warnings=len(ex.warnings))
    if ing:
        rep.stats["routes"] = ing.stats()
    store.log_run(started, len(mails), ex.llm_calls, len(ex.failures))
    if ex.failures:
        log.warning("%d mail(s) used the rule fallback. Run `mailbrief diagnose` for details.", len(ex.failures))

    if deliver:
        out = Path(cfg.output.dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"brief_{now:%Y%m%d_%H%M}"
        html_body = to_html(rep)
        if "md" in cfg.output.formats:
            (out / f"{stem}.md").write_text(to_markdown(rep), encoding="utf-8")
        if "html" in cfg.output.formats:
            (out / f"{stem}.html").write_text(html_body, encoding="utf-8")
        cards = out / "latest_cards.html"
        cards.write_text(to_cards_html(rep), encoding="utf-8")
        rep.stats["cards_path"] = str(cards.resolve())
        rep.stats["cards_json"] = str(save_cards(build_cards(rep), out / "latest_cards.json",
                                                 title=f"메일 브리핑 {now:%m/%d %H:%M}",
                                                 full_report=str((out / f"{stem}.html").resolve())).resolve())
        if tracker:
            try:  # a problem in the story views must not stop the briefing
                rep.stats.update(write_story_views(cfg, tracker, now, ing.stats() if ing else None))
            except Exception:
                log.exception("story dashboard / folder export failed")
        if cfg.output.send_to_self:
            source.send_to_self(f"[메일 브리핑] {now:%m/%d} 놓친 일 {len(rep.missed)}건 · 일정 {len(rep.upcoming_events)}건",
                                html_body)
    return rep
