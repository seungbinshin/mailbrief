"""Story tracking: one story = one goal that needs tracking over time (not one mail).

A thread becomes a story only if ALL of these are true (facts from the LLM, decision by rules here):
  - persistence    work continues after the thread (more steps, waiting for others, several days)
  - owner          a person or team drives the goal (R&R)
  - end_condition  a sentence can say when the goal is done
  - confidence     the LLM confidence is at least stories.min_confidence

If a condition is missing or not sure, the thread goes to the REVIEW QUEUE. It does not become a story.
If the thread does not match an open story, nothing is promoted: "no match" is never a reason to make a story.

Routes (judge):
  story   all conditions present, no conflict  -> attach to the matched/linked story, or make a new story
  review  persistence, but a condition is missing, the confidence is low, or the category says "notice"
  none    no persistence (a one-off task, information, a notice, a newsletter, an automatic mail)
  (pending: the LLM failed for this thread. No decision now. The next run tries again.)

Review state is derived, not stored as a flag: a story with no owner or no end condition shows in the review
queue, and it leaves the queue when the owner and the end condition are set.

Story updates are append-only. Each update has the full source time and capture time (not only the date),
and a content hash. Only an exact copy (same thread revision and summary) is skipped. Merge does not delete.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .analyze import _similar
from .models import MailInsight, Thread
from .threads import is_mine

ROUTES = ("story", "review", "none")

REASON_KO = {
    "owner_unknown": "담당자 미확인",
    "end_unknown": "종결 조건 미확인",
    "low_confidence": "판정 확신 낮음",
    "category_conflict": "공지/뉴스레터 분류지만 후속 업무 있음",
    "reopened": "검토 제외 후 다시 스토리 후보로 판정",
    "possible_duplicate": "기존 스토리와 중복 가능",
    "end_reached": "종결 조건 충족 가능 — 종료 확인 필요",
    "new_activity": "종료된 스토리에 새 메일",
}


def reason_ko(code: str) -> str:
    base, _, arg = code.partition(":")
    text = REASON_KO.get(base, base)
    return f"{text} ({arg})" if arg else text


@dataclass
class Verdict:
    route: str                  # story | review | none
    reasons: list[str] = field(default_factory=list)
    noise: bool = False         # newsletter / notice category or automatic sender


def _local(addr: str) -> str:
    return addr.lower().split("@", 1)[0]


def is_noise(thread: Thread, ins: MailInsight, scfg, me: str = "") -> bool:
    if ins.category in scfg.non_story_categories:
        return True
    senders = {m.sender for m in thread.mails if not is_mine(m, me) and m.sender}
    pats = [re.compile(p, re.I) for p in scfg.auto_sender_patterns]
    return bool(senders) and all(any(p.search(_local(s)) for p in pats) for s in senders)


def judge(thread: Thread, ins: MailInsight, scfg, me: str = "") -> Verdict:
    """Deterministic story decision from the LLM facts. Not sure -> review. Never "story" by default."""
    noise = is_noise(thread, ins, scfg, me)
    s = ins.story
    if not ins.by_llm or not s.persistence:
        return Verdict("none", [], noise)
    reasons = []
    if noise:
        reasons.append("category_conflict")
    if not s.owner:
        reasons.append("owner_unknown")
    if not s.end_condition:
        reasons.append("end_unknown")
    if s.confidence < scfg.min_confidence:
        reasons.append("low_confidence")
    return Verdict("review" if reasons else "story", reasons, noise)


# ---------------------------------------------------------------- records

def _utc(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_now(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_id(v: str, prefix: str) -> int:
    s = str(v).strip().upper()
    if s.startswith(prefix):
        s = s[len(prefix):]
    if not s.isdigit():
        raise ValueError(f"bad id {v!r}: use {prefix}<number>, e.g. {prefix}3")
    return int(s)


@dataclass
class Story:
    id: int
    title: str
    goal: str
    owner: str
    end_condition: str
    status: str                 # active | done | merged
    flags: list[str]
    merged_into: int | None
    created: str
    updated: str

    @property
    def sid(self) -> str:
        return f"S{self.id}"

    def review_reasons(self) -> list[str]:
        r = []
        if self.status == "active":
            if not self.owner:
                r.append("owner_unknown")
            if not self.end_condition:
                r.append("end_unknown")
        return r + list(self.flags)

    @classmethod
    def row(cls, d: dict) -> "Story":
        return cls(d["id"], d["title"] or "", d["goal"] or "", d["owner"] or "", d["end_condition"] or "",
                   d["status"], json.loads(d["flags"] or "[]"), d["merged_into"], d["created"], d["updated"])


@dataclass
class ReviewItem:
    id: int
    thread_key: str
    keys: list[str]
    revision: str
    email_id: str
    subject: str
    summary: str
    category: str
    reasons: list[str]
    proposal: dict
    status: str                 # open | promoted | dismissed
    story_id: int | None
    source_time: str
    created: str
    updated: str

    @property
    def rid(self) -> str:
        return f"R{self.id}"

    @classmethod
    def row(cls, d: dict) -> "ReviewItem":
        return cls(d["id"], d["thread_key"], json.loads(d["keys"] or "[]"), d["revision"], d["email_id"],
                   d["subject"] or "", d["summary"] or "", d["category"] or "", json.loads(d["reasons"] or "[]"),
                   json.loads(d["proposal"] or "{}"), d["status"], d["story_id"], d["source_time"], d["created"],
                   d["updated"])


@dataclass
class IngestResult:
    created: list[tuple[Story, Thread]] = field(default_factory=list)
    attached: list[tuple[Story, Thread]] = field(default_factory=list)
    reviews: list[tuple[ReviewItem, Thread]] = field(default_factory=list)
    routes: Counter = field(default_factory=Counter)   # story / review / none / pending
    by_thread: dict[str, str] = field(default_factory=dict)   # thread key -> route

    def stats(self) -> dict:
        return {"threads": sum(self.routes.values()), **{k: self.routes.get(k, 0) for k in (*ROUTES, "pending")},
                "stories_created": len(self.created), "stories_updated": len(self.attached),
                "reviews_opened": len(self.reviews)}


# ---------------------------------------------------------------- tracker

class StoryTracker:
    def __init__(self, store, scfg, me: str = "") -> None:
        self.store, self.cfg, self.me = store, scfg, me

    # ---------- queries
    def get(self, sid, follow: bool = True) -> Story | None:
        i = parse_id(sid, "S") if isinstance(sid, str) else sid
        for _ in range(10):  # follow merged_into
            rows = self.store.query("SELECT * FROM stories WHERE id=?", (i,))
            if not rows:
                return None
            st = Story.row(rows[0])
            if not (follow and st.status == "merged" and st.merged_into):
                return st
            i = st.merged_into
        return st

    def stories(self, statuses=("active",)) -> list[Story]:
        q = ",".join("?" * len(statuses))
        return [Story.row(r) for r in self.store.query(
            f"SELECT * FROM stories WHERE status IN ({q}) ORDER BY updated DESC, id DESC", tuple(statuses))]

    def needs_review(self) -> list[Story]:
        return [s for s in self.stories(("active", "done")) if s.review_reasons()]

    def review(self, rid) -> ReviewItem | None:
        i = parse_id(rid, "R") if isinstance(rid, str) else rid
        rows = self.store.query("SELECT * FROM reviews WHERE id=?", (i,))
        return ReviewItem.row(rows[0]) if rows else None

    def open_reviews(self) -> list[ReviewItem]:
        return [ReviewItem.row(r) for r in self.store.query(
            "SELECT * FROM reviews WHERE status='open' ORDER BY source_time DESC, id DESC")]

    def updates(self, story_id: int, limit: int = 0) -> list[dict]:
        sql = ("SELECT * FROM story_updates WHERE story_id=? "
               "ORDER BY source_time DESC, captured_at DESC, id DESC")
        return self.store.query(sql + (f" LIMIT {int(limit)}" if limit else ""), (story_id,))

    def latest_update(self, story_id: int) -> dict | None:
        u = self.updates(story_id, 1)
        return u[0] if u else None

    def links(self, story_id: int) -> list[dict]:
        return self.store.query("SELECT * FROM story_links WHERE story_id=? ORDER BY last_seen DESC", (story_id,))

    def prompt_stories(self, limit: int | None = None) -> list[tuple[str, str, str]]:
        n = self.cfg.prompt_stories if limit is None else limit
        return [(s.sid, s.title, s.goal) for s in self.stories()[:n]]

    def linked_story(self, thread: Thread) -> Story | None:
        gap = timedelta(days=self.cfg.link_gap_days)
        for key in sorted(thread.keys | {thread.key}):
            rows = self.store.query("SELECT story_id, last_seen FROM story_links WHERE key=?", (key,))
            if not rows:
                continue
            if key.startswith("s:") and rows[0]["last_seen"]:
                # A subject-only link is weak: a later thread with the same generic subject is another thread.
                if thread.mails[0].received - datetime.fromisoformat(rows[0]["last_seen"]) > gap:
                    continue
            st = self.get(rows[0]["story_id"])
            if st:
                return st
        return None

    def _review_for(self, thread: Thread) -> ReviewItem | None:
        rows = self.store.query("SELECT * FROM reviews WHERE thread_key=?", (thread.key,))
        if rows:
            return ReviewItem.row(rows[0])
        for r in self.store.query("SELECT * FROM reviews WHERE status != 'promoted'"):
            item = ReviewItem.row(r)
            if set(item.keys) & thread.keys:
                return item
        return None

    # ---------- writes
    def _set_flags(self, st: Story, flags: list[str]) -> None:
        self.store._exec("UPDATE stories SET flags=? WHERE id=?", (json.dumps(sorted(set(flags))), st.id))
        st.flags = sorted(set(flags))

    def _link(self, story_id: int, keys, how: str, seen: str) -> None:
        for k in keys:
            self.store._exec("INSERT OR IGNORE INTO story_links VALUES (?,?,?,?)", (k, story_id, how, seen))
            self.store._exec("UPDATE story_links SET last_seen=? WHERE key=? AND story_id=? AND last_seen < ?",
                             (seen, k, story_id, seen))

    def _add_update(self, story_id: int, thread_key: str, revision: str, email_id: str, subject: str,
                    summary: str, source_time: str, now: datetime) -> bool:
        h = hashlib.sha1(f"{revision}|{summary}".encode()).hexdigest()[:16]
        cur = self.store._exec(
            "INSERT OR IGNORE INTO story_updates(story_id, thread_key, revision, email_id, subject, summary, "
            "source_time, captured_at, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (story_id, thread_key, revision, email_id, subject, summary, source_time, _utc_now(now), h))
        self.store._exec("UPDATE stories SET updated=? WHERE id=? AND (updated IS NULL OR updated < ?)",
                         (source_time, story_id, source_time))
        return cur.rowcount == 1

    def _attach(self, st: Story, thread: Thread, ins: MailInsight, how: str, now: datetime) -> bool:
        seen = _utc(thread.latest.received)
        self._link(st.id, sorted(thread.keys | {thread.key}), how, seen)
        new = self._add_update(st.id, thread.key, thread.revision, thread.latest.id, thread.latest.subject,
                               ins.summary, seen, now)
        if new and ins.story.done and st.status == "active":
            self._set_flags(st, st.flags + ["end_reached"])
        if new and st.status == "done":
            self._set_flags(st, st.flags + ["new_activity"])
        return new

    def _similar_story(self, title: str, goal: str, exclude: int | None = None) -> Story | None:
        best, score = None, 0.0
        for s in self.stories():
            if s.id == exclude:
                continue
            sc = max(_similar(title, s.title), _similar(f"{title} {goal}", f"{s.title} {s.goal}"))
            if sc > score:
                best, score = s, sc
        return best if score >= self.cfg.duplicate_similarity else None

    def _create(self, title: str, goal: str, owner: str, end: str, created: str, flags=()) -> Story:
        dup = self._similar_story(title, goal)
        fl = list(flags) + ([f"possible_duplicate:{dup.sid}"] if dup else [])
        cur = self.store._exec(
            "INSERT INTO stories(title, goal, owner, end_condition, status, flags, merged_into, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (title, goal, owner, end, "active", json.dumps(fl), None, created, created))
        return self.get(cur.lastrowid)

    def _upsert_review(self, thread: Thread, ins: MailInsight, reasons: list[str], prev: ReviewItem | None,
                       now: datetime) -> ReviewItem:
        s = ins.story
        proposal = {"title": s.title or thread.subject, "goal": s.goal, "owner": s.owner,
                    "end_condition": s.end_condition, "confidence": s.confidence}
        vals = (json.dumps(sorted(thread.keys | {thread.key})), thread.revision, thread.latest.id,
                thread.latest.subject, ins.summary, ins.category, json.dumps(reasons, ensure_ascii=False),
                json.dumps(proposal, ensure_ascii=False), "open", _utc(thread.latest.received), _utc_now(now))
        if prev:
            self.store._exec("UPDATE reviews SET keys=?, revision=?, email_id=?, subject=?, summary=?, category=?, "
                             "reasons=?, proposal=?, status=?, source_time=?, updated=?, thread_key=? WHERE id=?",
                             (*vals, thread.key, prev.id))
            return self.review(prev.id)
        cur = self.store._exec(
            "INSERT INTO reviews(keys, revision, email_id, subject, summary, category, reasons, proposal, status, "
            "source_time, updated, thread_key, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*vals, thread.key, _utc_now(now)))
        return self.review(cur.lastrowid)

    def _close_review(self, item: ReviewItem | None, status: str, story_id: int | None, now: datetime) -> None:
        if item and item.status != status:
            self.store._exec("UPDATE reviews SET status=?, story_id=?, updated=? WHERE id=?",
                             (status, story_id, _utc_now(now), item.id))

    def ingest(self, threads: list[Thread], insights: dict[str, MailInsight], now: datetime) -> IngestResult:
        res = IngestResult()
        for t in threads:
            ins = insights.get(t.key)
            if ins is None:
                continue
            route = self._ingest_one(t, ins, now, res)
            res.routes[route] += 1
            res.by_thread[t.key] = route
        return res

    def _ingest_one(self, t: Thread, ins: MailInsight, now: datetime, res: IngestResult) -> str:
        if not ins.by_llm:
            return "pending"  # LLM failed: no decision. The fallback result is not cached, so the next run retries.
        linked = self.linked_story(t)
        if linked:
            if self._attach(linked, t, ins, "thread", now):
                res.attached.append((linked, t))
            return "story"
        v = judge(t, ins, self.cfg, self.me)
        prev = self._review_for(t)
        match = self.get(ins.story.match_id) if ins.story.match_id else None
        if match and match.status == "active" and not v.noise:
            self._attach(match, t, ins, "match", now)
            self._close_review(prev if prev and prev.status == "open" else None, "promoted", match.id, now)
            res.attached.append((match, t))
            return "story"
        if v.route == "story":
            if prev and prev.status == "dismissed":
                item = self._upsert_review(t, ins, ["reopened"], prev, now)
                res.reviews.append((item, t))
                return "review"
            s = ins.story
            st = self._create(s.title or t.subject, s.goal, s.owner, s.end_condition, _utc(t.latest.received))
            self._attach(st, t, ins, "new", now)
            self._close_review(prev if prev and prev.status == "open" else None, "promoted", st.id, now)
            res.created.append((self.get(st.id), t))
            return "story"
        if v.route == "review":
            if prev and prev.status == "dismissed":
                return "none"  # the user said "not a story" for this thread
            if prev and prev.status == "open" and prev.revision == t.revision:
                return "review"
            item = self._upsert_review(t, ins, v.reasons, prev, now)
            if not prev or prev.status != "open":
                res.reviews.append((item, t))
            return "review"
        return "none"

    # ---------- user actions (CLI)
    def new(self, title: str, now: datetime, goal: str = "", owner: str = "", end: str = "") -> Story:
        """Register a story by hand (e.g. move a real story from an old tracker). Threads attach by LLM match."""
        if not title.strip():
            raise ValueError("title is required")
        return self._create(title.strip(), goal.strip(), owner.strip(), end.strip(), _utc(now))

    def promote(self, rid, now: datetime, title: str = "", owner: str = "", end: str = "", goal: str = "",
                into: str = "") -> Story:
        item = self.review(rid)
        if not item:
            raise ValueError(f"no review item {rid}")
        p = item.proposal
        if into:
            st = self.get(into)
            if not st:
                raise ValueError(f"no story {into}")
        else:
            st = self._create(title or p.get("title") or item.subject, goal or p.get("goal", ""),
                              owner or p.get("owner", ""), end or p.get("end_condition", ""), item.source_time)
        self._link(st.id, item.keys or [item.thread_key], "promote", item.source_time)
        self._add_update(st.id, item.thread_key, item.revision, item.email_id, item.subject, item.summary,
                         item.source_time, now)
        self._close_review(item, "promoted", st.id, now)
        return self.get(st.id)

    def dismiss(self, rid, now: datetime) -> ReviewItem:
        item = self.review(rid)
        if not item:
            raise ValueError(f"no review item {rid}")
        self._close_review(item, "dismissed", None, now)
        return self.review(item.id)

    def set_fields(self, sid, **fields) -> Story:
        st = self._need(sid)
        allowed = {"title", "goal", "owner", "end_condition"}
        for k, v in fields.items():
            if k in allowed and v is not None:
                self.store._exec(f"UPDATE stories SET {k}=? WHERE id=?", (v.strip(), st.id))
        return self.get(st.id)

    def confirm(self, sid) -> Story:
        """Clear the stored review flags (possible_duplicate, end_reached, new_activity)."""
        st = self._need(sid)
        self._set_flags(st, [])
        return self.get(st.id)

    def close(self, sid) -> Story:
        st = self._need(sid)
        self.store._exec("UPDATE stories SET status='done', flags='[]' WHERE id=?", (st.id,))
        return self.get(st.id)

    def reopen(self, sid) -> Story:
        st = self._need(sid)
        self.store._exec("UPDATE stories SET status='active', flags='[]' WHERE id=?", (st.id,))
        return self.get(st.id)

    def merge(self, src_id, dst_id) -> Story:
        src, dst = self._need(src_id), self._need(dst_id)
        if src.id == dst.id:
            raise ValueError("cannot merge a story into itself")
        st = self.store
        st._exec("UPDATE story_links SET story_id=? WHERE story_id=?", (dst.id, src.id))
        st._exec("UPDATE OR IGNORE story_updates SET story_id=? WHERE story_id=?", (dst.id, src.id))
        st._exec("DELETE FROM story_updates WHERE story_id=?", (src.id,))  # only exact copies remain here
        st._exec("UPDATE reviews SET story_id=? WHERE story_id=?", (dst.id, src.id))
        for k in ("goal", "owner", "end_condition"):
            if not getattr(dst, k) and getattr(src, k):
                st._exec(f"UPDATE stories SET {k}=? WHERE id=?", (getattr(src, k), dst.id))
        st._exec("UPDATE stories SET updated=? WHERE id=? AND updated < ?", (src.updated, dst.id, src.updated))
        st._exec("UPDATE stories SET status='merged', merged_into=?, flags='[]' WHERE id=?", (dst.id, src.id))
        self._set_flags(dst, [f for f in dst.flags if f != f"possible_duplicate:{src.sid}"])
        return self.get(dst.id)

    def unlink(self, sid, key: str) -> int:
        st = self._need(sid)
        return self.store._exec("DELETE FROM story_links WHERE key=? AND story_id=?", (key, st.id)).rowcount

    def _need(self, sid) -> Story:
        st = self.get(sid)
        if not st:
            raise ValueError(f"no story {sid}")
        return st
