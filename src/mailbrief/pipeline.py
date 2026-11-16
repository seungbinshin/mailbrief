"""End-to-end run: fetch -> extract (LLM) -> analyze (rules) -> brief (LLM) -> render -> deliver.

Read and unread mails are both processed: the source does not filter on the read flag.
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
from .models import Email, MailInsight
from .prompts import BRIEF_SYSTEM
from .cards import build_cards, save_cards, to_cards_html
from .report import to_html, to_markdown
from .sources import MailSource, make_source
from .store import Store

log = logging.getLogger(__name__)


def make_extractor(cfg: Config, llm, store: Store | None) -> Extractor:
    a = cfg.analysis
    debug = Path(cfg.output.dir) / "debug" if a.debug_dump else None
    return Extractor(llm, store, cfg.llm.model, cfg.source.my_address or cfg.source.email, a.timezone,
                     a.max_body_chars, a.categories, a.category_aliases, a.retry_body_chars, a.min_body_chars, debug)


def analyze_mails(cfg: Config, mails: list[Email], llm, store: Store, workers: int = 4) -> tuple[dict[str, MailInsight], Extractor]:
    """Extract insights for all mails. Uses the cache; LLM calls run in parallel threads."""
    skip = {s.lower() for s in cfg.analysis.skip_senders}
    targets = [m for m in mails if m.sender.lower() not in skip]
    ex = make_extractor(cfg, llm, None)  # no store inside threads
    insights: dict[str, MailInsight] = {}
    todo = []
    for m in targets:
        c = store.get_insight(m.id, ex.cache_key)
        if c:
            insights[m.id] = c
        else:
            todo.append(m)
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for ins in pool.map(ex.analyze, todo):
                insights[ins.email_id] = ins
                if ins.by_llm and llm is not None:
                    store.put_insight(ins, ex.cache_key)
    for f in ex.failures:
        store.add_failure(f)
    return insights, ex


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
    mails = source.fetch_mails(now - timedelta(days=a.lookback_days), cfg.source.max_mails)
    events = source.fetch_events(now - timedelta(days=a.lookback_days), now + timedelta(days=a.lookahead_days))
    log.info("fetched %d mails (%d already read), %d events",
             len(mails), sum(m.is_read for m in mails), len(events))

    insights, ex = analyze_mails(cfg, mails, llm, store, workers)
    me = cfg.source.my_address or cfg.source.email
    rep = build_report(now, mails, insights, events, a, me, store.done_keys())
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
        if cfg.output.send_to_self:
            source.send_to_self(f"[메일 브리핑] {now:%m/%d} 놓친 일 {len(rep.missed)}건 · 일정 {len(rep.upcoming_events)}건",
                                html_body)
    return rep
