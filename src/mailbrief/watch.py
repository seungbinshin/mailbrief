"""Watch mode: process each new mail when it arrives, and run the full briefing at fixed times.

- Trigger 1: Outlook NewMailEx event (COM source, Windows). Reaction within seconds.
- Trigger 2: polling every `watch.poll_minutes` (all sources; also catches mails that the event missed,
  e.g. mails that arrived while the PC was asleep).
- Read state does not matter: a mail that you already read in Outlook is processed the same way.
- A new mail is analyzed with its whole thread (1 LLM call per thread revision, SQLite cache) and notified
  at most once (`notified` table). The thread result also updates the story tracker.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .cards import Card, CardItem, save_cards
from .config import Config, expand
from .llm import OpenAICompatClient
from .models import Email, MailInsight
from .pipeline import analyze_threads, fetch_threads, make_tracker, run
from .sources import make_source
from .store import Store

log = logging.getLogger(__name__)


def should_notify(mail: Email, ins: MailInsight, cfg: Config, now: datetime) -> str:
    """Return the reason for a popup, or "" for no popup."""
    w = cfg.watch
    if ins.priority <= w.notify_priority:
        return "중요"
    limit = now + timedelta(hours=w.notify_due_hours)
    for a in ins.action_items:
        if a.owner != "other" and a.due and a.due <= limit:
            return f"마감 {a.due:%m/%d %H:%M}"
    if mail.is_meeting_request:
        return "회의 초대"
    return ""


def notify_cards(items: list[tuple[Email, MailInsight, str]]) -> list[Card]:
    card = Card("새 메일 알림", "#dc2626")
    card.items = [CardItem(m.subject, f"{m.sender_name or m.sender} · {i.summary}", reason) for m, i, reason in items]
    return [card]


def open_popup(cards_json: Path) -> None:
    """Show the popup in a separate process, so the watch loop does not stop."""
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        w = exe[:-10] + "pythonw.exe"
        exe = w if Path(w).exists() else exe  # pythonw: no console window
    subprocess.Popen([exe, "-m", "mailbrief", "popup", str(cards_json)], close_fds=True)


def _process_new(cfg: Config, new: list[Email], source, store: Store, llm,
                 now: datetime) -> list[tuple[Email, MailInsight, str]]:
    """Analyze the threads of the new mails (with the older mails of the thread) and update the stories."""
    _, threads = fetch_threads(cfg, source, now - timedelta(days=cfg.analysis.lookback_days))
    new_ids = {m.id for m in new}
    hit = [t for t in threads if new_ids & set(t.ids)]
    tracker = make_tracker(cfg, store)
    insights, _ = analyze_threads(cfg, hit, llm, store, open_stories=tracker.prompt_stories() if tracker else [])
    if tracker:
        tracker.ingest(hit, insights, now)
    out = []
    for t in hit:  # one popup line per thread, for its newest new mail
        ins = insights.get(t.key)
        m = next((x for x in reversed(t.mails) if x.id in new_ids), None)
        r = should_notify(m, ins, cfg, now) if (ins and m) else ""
        if r:
            out.append((m, ins, r))
    return out


class _ComEvents:
    """Flag that Outlook sets on NewMailEx."""
    fired = False

    def OnNewMailEx(self, entry_ids):  # noqa: N802 (COM event name)
        _ComEvents.fired = True


def _com_pump():
    try:
        import pythoncom
        import win32com.client
        win32com.client.DispatchWithEvents("Outlook.Application", _ComEvents)
        log.info("listening to Outlook NewMailEx events")
        return pythoncom.PumpWaitingMessages
    except Exception as e:  # not Windows, pywin32 missing, Outlook not running
        log.info("Outlook events not available (%s): polling only", e)
        return None


def watch(cfg: Config, once: bool = False, popup: bool = True, llm=None, source=None, store=None,
          clock=None) -> list[tuple[Email, MailInsight, str]]:
    """Run the loop. With once=True: one poll, return the notified mails (used by tests)."""
    tz = ZoneInfo(cfg.analysis.timezone)
    clock = clock or (lambda: datetime.now(tz))
    source = source or make_source(cfg.source, tz)
    store = store or Store(expand(cfg.output.db_path))
    llm = llm or OpenAICompatClient(cfg.llm)
    notified: list = []
    pump = _com_pump() if (cfg.source.type == "com" and cfg.watch.use_com_events and not once) else None
    started = clock()
    # At start, look back one poll window + 1 hour. Older mails go to the briefing, not to popups.
    since = started - timedelta(minutes=cfg.watch.poll_minutes, hours=1)
    next_poll = 0.0
    done_briefs: set[str] = set()
    tmp = Path(tempfile.gettempdir())

    while True:
        now = clock()
        # Scheduled full briefing (e.g. brief_times = ["08:30", "13:30"])
        for hhmm in ([] if once else cfg.watch.brief_times):
            h, m = map(int, hhmm.split(":"))
            slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
            tag = f"{now:%Y%m%d}-{hhmm}"
            if slot <= now < slot + timedelta(hours=2) and tag not in done_briefs:
                done_briefs.add(tag)
                try:
                    rep = run(cfg, source=source, store=store, llm=llm)
                    if popup and cfg.output.popup and rep.stats.get("cards_json"):
                        open_popup(Path(rep.stats["cards_json"]))
                except Exception:
                    log.exception("scheduled briefing failed")

        if pump:
            pump()
        if _ComEvents.fired or time.monotonic() >= next_poll:
            _ComEvents.fired = False
            next_poll = time.monotonic() + cfg.watch.poll_minutes * 60
            try:
                mails = source.fetch_mails(since, cfg.source.max_mails)
                new = [m for m in mails if store.mark_notified(m.id)]  # first time seen (read or unread)
                if new:
                    hits = _process_new(cfg, new, source, store, llm, now)
                    notified.extend(hits)
                    log.info("new mails: %d, notify: %d", len(new), len(hits))
                    if hits and popup:
                        p = save_cards(notify_cards(hits), tmp / f"mailbrief_new_{now:%H%M%S}.json",
                                       title=f"새 메일 {now:%H:%M}")
                        open_popup(p)
                since = max(since, now - timedelta(minutes=cfg.watch.poll_minutes, hours=1))
            except Exception:
                log.exception("poll failed; retry at next poll")
        if once:
            return notified
        time.sleep(1 if pump else 5)
