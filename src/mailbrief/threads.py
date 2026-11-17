"""Group mails into threads (conversations) and remove copies.

Rules, in this order:
  1. Remove copies. Same Message-ID, or the same sender + subject + received minute + body start.
     The inbox copy is kept, the sent copy is removed.
  2. Mails with the same conversation_id are one thread.
  3. Mails with the same subject after RE:/FW:/회신:/전달: prefixes are removed are one thread, but only
     when they have a common participant and are at most `gap_days` apart. This covers sources without
     conversation_id and Outlook conversations that broke (for example a reply from a mobile client).
  4. A thread must have at least one inbox mail. A thread of only sent mails is not analyzed.

One thread is one LLM call. "RE: RE: 설계 리뷰" is not a new mail: it is a new revision of the thread.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from .models import Email, Thread

_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:re|fw|fwd|aw|wg|sv|vs|antw|tr|회신|답장|답신|전달)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]"
    r"|\[(?:ext|external|외부|외부메일)\]|\((?:ext|external|외부)\)"
    r")\s*",
    re.IGNORECASE)


def normalize_subject(subject: str) -> str:
    """'RE: FW: [EXT] 설계 리뷰' -> '설계 리뷰'."""
    s, prev = subject or "", None
    while prev != s:
        prev = s
        s = _PREFIX_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def subject_key(subject: str) -> str:
    return "s:" + hashlib.sha1(normalize_subject(subject).lower().encode()).hexdigest()[:12]


def is_mine(mail: Email, me: str) -> bool:
    return mail.folder == "sent" or bool(me and mail.sender.lower() == me.lower())


def _copy_key(m: Email) -> str:
    if m.message_id:
        return "mid:" + m.message_id.strip().strip("<>").lower()
    body = hashlib.sha1(re.sub(r"\s+", " ", m.body_text or "")[:300].encode()).hexdigest()[:10]
    return f"{m.sender.lower()}|{normalize_subject(m.subject).lower()}|{m.received:%Y%m%d%H%M}|{body}"


def remove_copies(mails: list[Email]) -> list[Email]:
    seen: dict[str, Email] = {}
    for m in sorted(mails, key=lambda x: x.folder != "inbox"):  # inbox copies first
        seen.setdefault(_copy_key(m), m)
    return list(seen.values())


def _participants(m: Email, me: str) -> set[str]:
    p = {a.lower() for a in [m.sender, *m.to, *m.cc] if a}
    p.discard(me.lower() if me else "")
    return p


class _Union:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def join(self, a: int, b: int) -> None:
        self.p[self.find(a)] = self.find(b)


def group_threads(mails: list[Email], me: str = "", skip_senders: list[str] | None = None,
                  gap_days: int = 14) -> list[Thread]:
    """Return threads, newest activity first."""
    skip = {s.lower() for s in (skip_senders or [])}
    ms = sorted(remove_copies([m for m in mails if m.sender.lower() not in skip]), key=lambda m: m.received)
    u = _Union(len(ms))

    by_conv: dict[str, int] = {}
    for i, m in enumerate(ms):
        if m.conversation_id:
            if m.conversation_id in by_conv:
                u.join(i, by_conv[m.conversation_id])
            else:
                by_conv[m.conversation_id] = i

    by_subj: dict[str, list[int]] = {}
    for i, m in enumerate(ms):
        s = normalize_subject(m.subject).lower()
        if s:
            by_subj.setdefault(s, []).append(i)
    gap = timedelta(days=gap_days)
    for idx in by_subj.values():
        for k, i in enumerate(idx):
            pi = _participants(ms[i], me)
            for j in idx[:k]:
                if ms[i].received - ms[j].received <= gap and (pi & _participants(ms[j], me)):
                    u.join(i, j)

    groups: dict[int, list[Email]] = {}
    for i, m in enumerate(ms):
        groups.setdefault(u.find(i), []).append(m)

    out: list[Thread] = []
    used: set[str] = set()
    for g in sorted(groups.values(), key=lambda g: g[0].received):
        if not any(m.folder == "inbox" for m in g):
            continue
        convs = [m.conversation_id for m in g if m.conversation_id]
        keys = {f"c:{c}" for c in convs}
        skey = subject_key(g[0].subject) if normalize_subject(g[0].subject) else f"m:{g[0].id}"
        if skey in used:  # same subject, other people: a different thread
            skey = f"{skey}:{hashlib.sha1(g[0].id.encode()).hexdigest()[:6]}"
        if len(convs) < len(g):  # at least one mail without conversation id
            keys.add(skey)
        key = f"c:{convs[0]}" if convs else skey
        used.add(skey)
        subj = normalize_subject(g[-1].subject) or normalize_subject(g[0].subject) or "(제목 없음)"
        out.append(Thread(key=key, subject=subj, mails=g, keys=keys))
    out.sort(key=lambda t: t.latest.received, reverse=True)
    return out
