"""Rule-based analysis on top of LLM insights: daily digest, missed items, upcoming items.

The LLM only extracts facts per mail. All decisions about "missed" and "upcoming" are
deterministic, so the result is predictable and testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import (RESP_DECLINED, RESP_NONE, RESP_NOT_RESPONDED, ActionItem, Email, Event, MailInsight,
                     MentionedEvent)


@dataclass
class DigestEntry:
    mail: Email
    insight: MailInsight


@dataclass
class Finding:
    kind: str       # unanswered_invite | overdue_action | missed_mail_event | overdue_reply | ...
    title: str
    when: datetime | None
    detail: str = ""
    ref_id: str = ""
    key: str = ""   # action item key (for `mailbrief done KEY`)


@dataclass
class Report:
    generated: datetime
    digest: list[DigestEntry] = field(default_factory=list)
    missed: list[Finding] = field(default_factory=list)
    attention: list[Finding] = field(default_factory=list)  # needs a decision now (pending invites)
    upcoming_events: list[Event] = field(default_factory=list)
    upcoming: list[Finding] = field(default_factory=list)   # deadlines and mail-only events ahead
    briefing: str = ""
    stats: dict = field(default_factory=dict)


def _norm(s: str) -> set[str]:
    s = re.sub(r"^(re|fw|fwd|회신|전달)\s*:\s*", "", s.strip(), flags=re.I)
    return {t for t in re.findall(r"[0-9a-zA-Z가-힣]+", s.lower()) if len(t) > 1}


def _similar(a: str, b: str) -> float:
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def on_calendar(m: MentionedEvent, events: list[Event], tol_min: int) -> bool:
    tol = timedelta(minutes=tol_min)
    for e in events:
        if e.is_cancelled:
            continue
        same_time = abs(e.start - m.start) <= tol
        overlaps_day = e.is_all_day and e.start.date() <= m.start.date() <= (e.end - timedelta(seconds=1)).date()
        if (same_time or overlaps_day) and (_similar(m.title, e.subject) >= 0.3 or same_time):
            return True
    return False


def dedup_actions(items: list[ActionItem]) -> list[ActionItem]:
    seen: dict[str, ActionItem] = {}
    for a in items:
        seen.setdefault(a.key, a)
    return list(seen.values())


def dedup_events(items: list[MentionedEvent], tol_min: int) -> list[MentionedEvent]:
    out: list[MentionedEvent] = []
    for m in sorted(items, key=lambda x: x.start):
        if any(abs(o.start - m.start) <= timedelta(minutes=tol_min) and _similar(o.title, m.title) >= 0.5
               for o in out):
            continue
        out.append(m)
    return out


def build_report(now: datetime, mails: list[Email], insights: dict[str, MailInsight], events: list[Event],
                 cfg, me: str, done_keys: set[str]) -> Report:
    a = cfg
    rep = Report(generated=now)
    digest_from = now - timedelta(hours=a.digest_hours)
    look_from = now - timedelta(days=a.lookback_days)
    ahead_to = now + timedelta(days=a.lookahead_days)
    skip = {s.lower() for s in a.skip_senders}
    mails = [m for m in mails if m.sender.lower() not in skip]

    # 1) Daily digest: mails in the digest window, sorted by priority then time.
    rep.digest = [DigestEntry(m, insights[m.id]) for m in mails if m.received >= digest_from and m.id in insights]
    rep.digest.sort(key=lambda d: (d.insight.priority, -d.mail.received.timestamp()))

    # 2) Calendar based checks.
    for e in events:
        if e.is_cancelled or e.is_organizer or not e.response_requested:
            continue
        if e.response_status in (RESP_NONE, RESP_NOT_RESPONDED):
            f = Finding("unanswered_invite", e.subject, e.start, f"주최: {e.organizer}".strip(), e.id)
            if e.start < now and e.start >= look_from:
                f.detail = "응답하지 않은 채 지난 회의. " + f.detail
                rep.missed.append(f)
            elif now <= e.start <= ahead_to:
                rep.attention.append(f)

    rep.upcoming_events = [e for e in events
                           if now <= e.end and e.start <= ahead_to and not e.is_cancelled
                           and e.response_status != RESP_DECLINED]

    # 3) Action items from mails in the lookback window.
    window_mails = {m.id: m for m in mails if m.received >= look_from}
    actions = dedup_actions([ai for mid in window_mails if mid in insights for ai in insights[mid].action_items])
    for ai in actions:
        if ai.owner == "other" or ai.key in done_keys or ai.due is None:
            continue
        f = Finding("overdue_action" if ai.due < now else "upcoming_deadline", ai.description, ai.due,
                    f"출처: {ai.source_subject}", ai.source_email_id, ai.key)
        if ai.due < now:
            rep.missed.append(f)
        elif ai.due <= ahead_to:
            rep.upcoming.append(f)

    # 4) Events mentioned in mail but missing on the calendar.
    mentioned = dedup_events([me_ for mid in window_mails if mid in insights for me_ in insights[mid].mentioned_events],
                             a.event_match_minutes)
    for m in mentioned:
        if on_calendar(m, events, a.event_match_minutes):
            continue
        f = Finding("mail_event_not_on_calendar", m.title, m.start,
                    f"메일에만 있고 캘린더에 없음. 출처: {m.source_subject}" + (f" / 장소: {m.location}" if m.location else ""),
                    m.source_email_id)
        if look_from <= m.start < now:
            f.kind = "missed_mail_event"
            rep.missed.append(f)
        elif now <= m.start <= ahead_to:
            rep.upcoming.append(f)

    # 5) Mails that need a reply and are old enough.
    reply_limit = now - timedelta(hours=a.reply_overdue_hours)
    overdue_by_mail = {f.ref_id: f for f in rep.missed if f.kind == "overdue_action"}
    for mid, m in window_mails.items():
        ins = insights.get(mid)
        if ins and ins.needs_reply and mid in overdue_by_mail:  # one entry per mail: merge into the action
            overdue_by_mail[mid].detail += " · 회신 필요"
            continue
        if ins and ins.needs_reply and m.received <= reply_limit and (me.lower() in [t.lower() for t in m.to] or not me):
            rep.missed.append(Finding("overdue_reply", m.subject, m.received,
                                      f"{m.sender_name or m.sender} — 회신 필요 ({ins.summary})", mid))

    key = lambda f: f.when or now
    rep.missed.sort(key=key)
    rep.attention.sort(key=key)
    rep.upcoming.sort(key=key)
    rep.stats = {
        "mails_total": len(mails), "digest": len(rep.digest), "missed": len(rep.missed),
        "attention": len(rep.attention), "upcoming_events": len(rep.upcoming_events),
        "upcoming_other": len(rep.upcoming),
        "llm_fallback": sum(1 for d in rep.digest if not d.insight.by_llm),
    }
    return rep


def briefing_facts(rep: Report, limit: int = 15) -> list[str]:
    facts = []
    for f in rep.missed[:5]:
        facts.append(f"[놓침/{f.kind}] {f.title} ({f.when:%m-%d %H:%M})" if f.when else f"[놓침] {f.title}")
    for f in rep.attention[:3]:
        facts.append(f"[응답 필요 초대] {f.title} ({f.when:%m-%d %H:%M})")
    today = rep.generated.date()
    for e in rep.upcoming_events:
        if e.start.date() == today:
            facts.append(f"[오늘 일정] {e.start:%H:%M} {e.subject}")
    for f in rep.upcoming[:5]:
        facts.append(f"[다가오는 마감/일정] {f.title} ({f.when:%m-%d %H:%M})")
    for d in rep.digest[:5]:
        if d.insight.priority == 1:
            facts.append(f"[중요 메일] {d.mail.subject}: {d.insight.summary}")
    return facts[:limit]


def rule_briefing(rep: Report) -> str:
    lines = []
    if rep.missed:
        lines.append(f"- 놓친 항목 {len(rep.missed)}건. 첫 항목: {rep.missed[0].title}")
    today = [e for e in rep.upcoming_events if e.start.date() == rep.generated.date()]
    if today:
        lines.append(f"- 오늘 일정 {len(today)}건. 첫 일정: {today[0].start:%H:%M} {today[0].subject}")
    high = [d for d in rep.digest if d.insight.priority == 1]
    if high:
        lines.append(f"- 중요 메일 {len(high)}건. 첫 메일: {high[0].mail.subject}")
    return "\n".join(lines) or "- 특이사항 없음."
