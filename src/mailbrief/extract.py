"""Per-thread extraction with the LLM. One LLM call per thread revision (not per mail).

Flow for one thread:
  1. no subject and no body in all mails -> rules, no LLM call
  2. LLM call with the thread: the first message + the latest N-1 messages, quoted history removed
  3. on E_CONTEXT / E_TRUNCATED / E_BAD_JSON / E_NO_JSON / E_EMPTY -> one retry with a shorter budget
  4. normalize (category and meeting type aliases, priority words, bad dates, story facts) and validate
  5. still failing -> rules fallback, failure is logged with a code (see `mailbrief diagnose`).
     A fallback result never becomes a story: the story facts stay empty.

The cache key is the thread revision (hash of the mail ids). A new reply makes a new revision.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import DEFAULT_MEETING_TYPES
from .llm import E_BAD_JSON, E_CONTEXT, E_EMPTY, E_NO_JSON, E_SCHEMA, E_TRUNCATED, LLMError, parse_json
from .models import ActionItem, Email, MailInsight, MentionedEvent, StoryVerdict, Thread
from .preprocess import clean_body
from .prompts import MESSAGE, OPEN_STORIES_HEAD, PROMPT_VERSION, THREAD_HEAD, extract_system
from .threads import is_mine, normalize_subject, subject_key

log = logging.getLogger(__name__)

_RETRY_SHORTER = {E_CONTEXT, E_TRUNCATED, E_BAD_JSON, E_NO_JSON, E_EMPTY}

# Words a model often outputs instead of the exact label.
_BUILTIN_ALIASES = {
    "action": "action_required", "action required": "action_required", "todo": "action_required",
    "request": "action_required", "task": "action_required", "조치 필요": "action_required", "조치필요": "action_required",
    "요청": "action_required", "할일": "action_required", "할 일": "action_required",
    "meeting_invite": "meeting", "invite": "meeting", "calendar": "meeting", "회의": "meeting", "일정": "meeting",
    "info": "fyi", "information": "fyi", "reference": "fyi", "참고": "fyi", "공유": "fyi",
    "notice": "announcement", "공지": "announcement", "안내": "announcement",
    "promotion": "newsletter", "ad": "newsletter", "spam": "newsletter", "뉴스레터": "newsletter", "광고": "newsletter",
    "etc": "other", "기타": "other", "unknown": "other",
}
_MEETING_ALIASES = {
    "정기": "regular", "정기회의": "regular", "weekly": "regular", "sync": "regular", "standup": "regular",
    "recurring": "regular", "리뷰": "review", "검토": "review", "보고": "review", "design review": "review",
    "결정": "decision", "의사결정": "decision", "승인": "decision", "approval": "decision",
    "외부": "external", "고객": "external", "customer": "external", "client": "external", "vendor": "external",
    "기타": "other", "etc": "other",
}
_PRIO_WORDS = {"high": 1, "urgent": 1, "높음": 1, "상": 1, "medium": 2, "normal": 2, "보통": 2, "중": 2,
               "low": 3, "낮음": 3, "하": 3}
_CONF_WORDS = {"high": 0.9, "높음": 0.9, "상": 0.9, "medium": 0.6, "mid": 0.6, "보통": 0.6, "중": 0.6,
               "low": 0.3, "낮음": 0.3, "하": 0.3}
# Values that mean "not known" for owner / end_condition.
_NULLISH = {"", "null", "none", "unknown", "n/a", "na", "tbd", "tba", "-", "?", "미정", "없음", "모름", "불명",
            "알 수 없음", "확인 필요", "not specified", "unspecified"}


def _parse_dt(v, tz: ZoneInfo) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    s = v.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?:[T ]\s*(\d{1,2}):(\d{2}))?", v.strip())
        if not m:
            return None
        y, mo, da, h, mi = m.groups()
        try:
            d = datetime(int(y), int(mo), int(da), int(h or 18), int(mi or 0))
        except ValueError:
            return None
    return d.replace(tzinfo=tz) if d.tzinfo is None else d


def _bool(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1", "예", "y")


def _text(v, min_len: int = 1) -> str:
    if v is None or isinstance(v, (bool, dict, list)):
        return ""
    s = str(v).strip().strip('"').strip()
    return "" if s.lower() in _NULLISH or len(s) < min_len else s


@dataclass
class Normalizer:
    categories: dict[str, str]
    aliases: dict[str, str] = field(default_factory=dict)
    meeting_types: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MEETING_TYPES))
    warnings: list[str] = field(default_factory=list)

    def category(self, v) -> str:
        fallback = "other" if "other" in self.categories else list(self.categories)[-1]
        if not isinstance(v, str):
            self.warnings.append(f"category not a string: {v!r}")
            return fallback
        k = v.strip().strip('"').lower()
        if k in self.categories:
            return k
        for src in (self.aliases, _BUILTIN_ALIASES):
            for a, target in src.items():
                if a.lower() == k and target in self.categories:
                    return target
        for c in self.categories:  # "action_required (조치)" -> action_required
            if k.startswith(c):
                return c
        self.warnings.append(f"unknown category {v!r} -> {fallback}")
        return fallback

    def meeting_type(self, v) -> str:
        k = _text(v).lower()
        if not k:
            return ""
        if k in self.meeting_types:
            return k
        for a, target in {**_MEETING_ALIASES, **{x.lower(): y for x, y in self.aliases.items()}}.items():
            if a == k and target in self.meeting_types:
                return target
        for c in self.meeting_types:
            if k.startswith(c):
                return c
        self.warnings.append(f"unknown meeting_type {v!r} -> ''")
        return ""

    def priority(self, v) -> int:
        if isinstance(v, str) and v.strip().lower() in _PRIO_WORDS:
            return _PRIO_WORDS[v.strip().lower()]
        try:
            return min(3, max(1, int(v)))
        except (TypeError, ValueError):
            self.warnings.append(f"bad priority {v!r} -> 2")
            return 2

    def confidence(self, v) -> float:
        if v is None:
            return 0.0
        if isinstance(v, str) and v.strip().lower() in _CONF_WORDS:
            return _CONF_WORDS[v.strip().lower()]
        try:
            f = float(v)
        except (TypeError, ValueError):
            self.warnings.append(f"bad story confidence {v!r} -> 0")
            return 0.0
        if 1 < f <= 100:  # percent
            f /= 100
        return min(1.0, max(0.0, f))

    def story(self, v, open_ids: set[str]) -> StoryVerdict:
        if v is None:
            return StoryVerdict()
        if not isinstance(v, dict):
            self.warnings.append(f"story not an object: {v!r}"[:120])
            return StoryVerdict()
        match = _text(v.get("match")).upper()
        if match and not match.startswith("S"):
            match = f"S{match}"
        if match and match not in open_ids:
            self.warnings.append(f"story match {v.get('match')!r} is not an open story -> ignored")
            match = ""
        return StoryVerdict(
            persistence=_bool(v.get("persistence", False)),
            owner=_text(v.get("owner")),
            end_condition=_text(v.get("end_condition"), min_len=4),
            title=_text(v.get("title"))[:80],
            goal=_text(v.get("goal"))[:200],
            confidence=self.confidence(v.get("confidence")),
            done=_bool(v.get("done", False)),
            match_id=match,
            reason=_text(v.get("reason"))[:200],
        )


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    return v if isinstance(v, list) else []


def as_thread(x: Email | Thread) -> Thread:
    if isinstance(x, Thread):
        return x
    key = f"c:{x.conversation_id}" if x.conversation_id else (
        subject_key(x.subject) if normalize_subject(x.subject) else f"m:{x.id}")
    return Thread(key=key, subject=normalize_subject(x.subject) or x.subject, mails=[x], keys={key})


def _msg_mail(thread: Thread, n) -> Email:
    try:
        i = int(n)
        if 1 <= i <= len(thread.mails):
            return thread.mails[i - 1]
    except (TypeError, ValueError):
        pass
    return thread.latest


def _flag_actions(thread: Thread, actions: list[ActionItem]) -> list[ActionItem]:
    out = []
    for m in thread.mails:
        if m.flag_due and not any(a.due and a.due.date() == m.flag_due.date() for a in actions + out):
            out.append(ActionItem(f"[플래그] {m.subject}", m.flag_due, "me", m.id, m.subject))
    return out


def insight_from_json(data: dict, thread: Email | Thread, tz: ZoneInfo, norm: Normalizer, me: str = "",
                      open_ids: set[str] | None = None) -> MailInsight:
    thread = as_thread(thread)
    latest = thread.latest
    if not isinstance(data.get("summary"), str) and "category" not in data:
        raise LLMError(f"JSON has no summary/category keys: {list(data)[:8]}", E_SCHEMA)
    actions = []
    for a in _as_list(data.get("action_items")):
        if isinstance(a, str):
            a = {"description": a}
        if not isinstance(a, dict) or not a.get("description"):
            continue
        owner = a.get("owner") if a.get("owner") in ("me", "other", "unknown") else "unknown"
        due = _parse_dt(a.get("due"), tz)
        if a.get("due") and due is None:
            norm.warnings.append(f"bad due {a.get('due')!r}")
        src = _msg_mail(thread, a.get("msg"))
        actions.append(ActionItem(str(a["description"]).strip(), due, owner, src.id, src.subject))
    events = []
    for e in _as_list(data.get("events")):
        if not isinstance(e, dict):
            continue
        start = _parse_dt(e.get("start"), tz)
        if not start:
            if e.get("start"):
                norm.warnings.append(f"bad event start {e.get('start')!r}")
            continue
        src = _msg_mail(thread, e.get("msg"))
        events.append(MentionedEvent(str(e.get("title") or thread.subject), start, _parse_dt(e.get("end"), tz),
                                     str(e.get("location") or ""), src.id, src.subject))
    actions += _flag_actions(thread, actions)
    # The latest message is mine: the ball is in the other court, no reply is due from me.
    needs_reply = _bool(data.get("needs_reply", False)) and not is_mine(latest, me)
    category = norm.category(data.get("category"))
    return MailInsight(
        email_id=latest.id,
        summary=str(data.get("summary") or latest.subject).strip(),
        category=category,
        priority=norm.priority(data.get("priority", 2)),
        needs_reply=needs_reply,
        action_items=actions,
        mentioned_events=events,
        by_llm=True,
        thread_key=thread.key,
        email_ids=thread.ids,
        meeting_type=norm.meeting_type(data.get("meeting_type")),
        story=norm.story(data.get("story"), open_ids or set()),
    )


_ACTION_WORDS = re.compile(r"(요청|부탁|회신|검토|제출|확인 바랍|please|could you|action required|by EOD|until)", re.I)
_NEWSLETTER = re.compile(r"(newsletter|unsubscribe|수신거부|광고|뉴스레터)", re.I)


def heuristic_insight(thread: Email | Thread, max_chars: int, categories: dict[str, str], fail_code: str = "",
                      me: str = "") -> MailInsight:
    """Rules only. The story facts stay empty: a fallback result is never a story."""
    thread = as_thread(thread)
    inbound = [m for m in thread.mails if not is_mine(m, me)] or thread.mails
    mail = inbound[-1]
    body = clean_body(mail.body_text, max_chars)
    text = f"{mail.subject}\n{body}"
    if any(m.is_meeting_request for m in thread.mails):
        cat = "meeting"
    elif _NEWSLETTER.search(text):
        cat = "newsletter"
    elif _ACTION_WORDS.search(text):
        cat = "action_required"
    else:
        cat = "fyi"
    if cat not in categories:
        cat = "other" if "other" in categories else list(categories)[-1]
    first = re.sub(r"\s+", " ", body)[:160]
    return MailInsight(
        email_id=thread.latest.id,
        summary=first or mail.subject,
        category=cat,
        priority=1 if mail.importance == "high" else (2 if cat == "action_required" else 3),
        needs_reply=cat == "action_required" and not is_mine(thread.latest, me),
        action_items=_flag_actions(thread, []),
        by_llm=False,
        fail_code=fail_code,
        thread_key=thread.key,
        email_ids=thread.ids,
    )


@dataclass
class Failure:
    email_id: str
    subject: str
    code: str
    message: str
    body_chars: int
    raw: str = ""
    warnings: list[str] = field(default_factory=list)


def _budgets(n: int, total: int) -> list[int]:
    """Body characters per message. Older messages share at most half of the budget; the latest gets the rest."""
    if n <= 1:
        return [total]
    older = max(200, min(800, total // (2 * (n - 1))))
    latest = max(total - older * (n - 1), min(total, 800))
    return [older] * (n - 1) + [latest]


class Extractor:
    """Thread-safe if `store` is None (pipeline writes the cache in the main thread)."""

    def __init__(self, llm, store, model_name: str, me: str, tz: str, max_chars: int,
                 categories: dict[str, str], aliases: dict[str, str] | None = None,
                 retry_chars: int = 1500, min_body_chars: int = 5, debug_dir: Path | None = None,
                 meeting_types: dict[str, str] | None = None, max_messages: int = 8) -> None:
        self.llm, self.store = llm, store
        self.meeting_types = meeting_types or dict(DEFAULT_MEETING_TYPES)
        self.cache_key = f"{model_name}|{PROMPT_VERSION}|{'/'.join(categories)}|{'/'.join(self.meeting_types)}"
        self.me, self.tzname, self.tz, self.max_chars = me, tz, ZoneInfo(tz), max_chars
        self.categories, self.aliases = categories, aliases or {}
        self.retry_chars, self.min_body_chars, self.debug_dir = retry_chars, min_body_chars, debug_dir
        self.max_messages = max(2, max_messages)
        self.system = extract_system(categories, me, tz, self.meeting_types)
        self.llm_calls = 0
        self.failures: list[Failure] = []
        self.warnings: dict[str, list[str]] = {}

    def _shown(self, thread: Thread) -> list[tuple[int, Email]]:
        numbered = list(enumerate(thread.mails, 1))
        if len(numbered) <= self.max_messages:
            return numbered
        return [numbered[0]] + numbered[-(self.max_messages - 1):]  # the first mail often has the request

    def user_prompt(self, thread: Email | Thread, max_chars: int,
                    open_stories: list[tuple[str, str, str]] = ()) -> str:
        thread = as_thread(thread)
        shown = self._shown(thread)
        omitted = len(thread.mails) - len(shown)
        parts = [THREAD_HEAD.format(shown=len(shown), total=len(thread.mails),
                                    omitted=f" {omitted} message(s) between [1] and [{shown[1][0]}] are omitted."
                                    if omitted else "")]
        for (n, m), budget in zip(shown, _budgets(len(shown), max_chars)):
            parts.append(MESSAGE.format(
                n=n, received=m.received.isoformat(),
                sender=f"{m.sender_name} <{m.sender}>", mine=" (owner)" if is_mine(m, self.me) else "",
                to=", ".join(m.to[:10]), cc=", ".join(m.cc[:10]), subject=m.subject,
                importance=m.importance, attachments="yes" if m.has_attachments else "no",
                meeting="yes" if m.is_meeting_request else "no",
                body=clean_body(m.body_text, budget) or "(empty body)"))
        if open_stories:
            parts.append(OPEN_STORIES_HEAD + "\n".join(
                f"- {sid}: {title}" + (f" — {goal}" if goal else "") for sid, title, goal in open_stories))
        return "".join(parts)

    def _call(self, user: str, last_try: bool = False) -> tuple[dict, str]:
        self.llm_calls += 1
        if hasattr(self.llm, "chat_ex"):
            text, finish = self.llm.chat_ex(self.system, user, json_output=True)
        else:
            text, finish = self.llm.chat(self.system, user, json_output=True), ""
        try:
            # On the last try, accept a cut output after closing the brackets (partial data > no data).
            return parse_json(text, allow_truncated=last_try), text
        except LLMError as e:
            if finish == "length":
                e.code = E_TRUNCATED
            raise

    def store_key(self, thread: Thread) -> str:
        return f"thr:{thread.revision}"

    def analyze(self, item: Email | Thread, open_stories: list[tuple[str, str, str]] = ()) -> MailInsight:
        thread = as_thread(item)
        if self.store:
            cached = self.store.get_insight(self.store_key(thread), self.cache_key)
            if cached:
                return cached
        bodies = [clean_body(m.body_text, self.max_chars) for m in thread.mails]
        empty = all(len(b) < self.min_body_chars for b in bodies) and not any(m.subject.strip() for m in thread.mails)
        if self.llm is None or empty:
            return heuristic_insight(thread, self.max_chars, self.categories, me=self.me)

        open_ids = {sid for sid, _, _ in open_stories}
        norm = Normalizer(self.categories, self.aliases, self.meeting_types)
        raw = ""
        last: LLMError | None = None
        for chars in (self.max_chars, self.retry_chars):
            user = self.user_prompt(thread, chars, open_stories)
            try:
                data, raw = self._call(user, last_try=chars == self.retry_chars)
                ins = insight_from_json(data, thread, self.tz, norm, self.me, open_ids)
                if data.get("_repaired_truncation"):
                    norm.warnings.append("output truncation repaired")
                if norm.warnings:
                    self.warnings[thread.latest.id] = norm.warnings
                if self.store:
                    self.store.put_insight(ins, self.cache_key, self.store_key(thread))
                return ins
            except LLMError as e:
                last, raw = e, e.raw or raw
                if e.code not in _RETRY_SHORTER or chars <= self.retry_chars:
                    break
                log.info("retry %s with shorter body (%s)", thread.key, e.code)
        f = Failure(thread.latest.id, thread.latest.subject, last.code, str(last)[:300], sum(map(len, bodies)),
                    raw[:2000], norm.warnings)
        self.failures.append(f)
        log.warning("LLM extraction failed for %s [%s] %s -> rule fallback", thread.key, f.code, f.message[:120])
        if self.debug_dir:
            self._dump(thread, f, open_stories)
        return heuristic_insight(thread, self.max_chars, self.categories, fail_code=last.code, me=self.me)

    def _dump(self, thread: Thread, f: Failure, open_stories) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", thread.latest.id)[-60:]
        (self.debug_dir / f"{safe}.txt").write_text(
            f"CODE: {f.code}\nMESSAGE: {f.message}\nWARNINGS: {f.warnings}\n\n=== SYSTEM ===\n{self.system}\n"
            f"=== USER ===\n{self.user_prompt(thread, self.max_chars, open_stories)}\n=== RAW OUTPUT ===\n{f.raw}\n",
            encoding="utf-8")
