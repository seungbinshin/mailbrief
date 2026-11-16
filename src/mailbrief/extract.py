"""Per-mail extraction with the LLM.

Flow for one mail:
  1. no subject and no body -> rules, no LLM call (empty meeting requests still have a subject)
  2. LLM call with the full cleaned body
  3. on E_CONTEXT / E_TRUNCATED / E_BAD_JSON / E_NO_JSON / E_EMPTY -> one retry with a shorter body
  4. normalize (category aliases, priority words, bad dates) and validate the shape
  5. still failing -> rules fallback, failure is logged with a code (see `mailbrief diagnose`)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .llm import E_BAD_JSON, E_CONTEXT, E_EMPTY, E_NO_JSON, E_SCHEMA, E_TRUNCATED, LLMError, parse_json
from .models import ActionItem, Email, MailInsight, MentionedEvent
from .preprocess import clean_body
from .prompts import EXTRACT_USER, PROMPT_VERSION, extract_system

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
_PRIO_WORDS = {"high": 1, "urgent": 1, "높음": 1, "상": 1, "medium": 2, "normal": 2, "보통": 2, "중": 2,
               "low": 3, "낮음": 3, "하": 3}


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
        d = datetime(int(y), int(mo), int(da), int(h or 18), int(mi or 0))
    return d.replace(tzinfo=tz) if d.tzinfo is None else d


@dataclass
class Normalizer:
    categories: dict[str, str]
    aliases: dict[str, str] = field(default_factory=dict)
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

    def priority(self, v) -> int:
        if isinstance(v, str) and v.strip().lower() in _PRIO_WORDS:
            return _PRIO_WORDS[v.strip().lower()]
        try:
            return min(3, max(1, int(v)))
        except (TypeError, ValueError):
            self.warnings.append(f"bad priority {v!r} -> 2")
            return 2


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    return v if isinstance(v, list) else []


def insight_from_json(data: dict, mail: Email, tz: ZoneInfo, norm: Normalizer) -> MailInsight:
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
        actions.append(ActionItem(str(a["description"]).strip(), due, owner, mail.id, mail.subject))
    events = []
    for e in _as_list(data.get("events")):
        if not isinstance(e, dict):
            continue
        start = _parse_dt(e.get("start"), tz)
        if not start:
            if e.get("start"):
                norm.warnings.append(f"bad event start {e.get('start')!r}")
            continue
        events.append(MentionedEvent(str(e.get("title") or mail.subject), start, _parse_dt(e.get("end"), tz),
                                     str(e.get("location") or ""), mail.id, mail.subject))
    if mail.flag_due and not any(a.due and a.due.date() == mail.flag_due.date() for a in actions):
        actions.append(ActionItem(f"[플래그] {mail.subject}", mail.flag_due, "me", mail.id, mail.subject))
    nr = data.get("needs_reply", False)
    return MailInsight(
        email_id=mail.id,
        summary=str(data.get("summary") or mail.subject).strip(),
        category=norm.category(data.get("category")),
        priority=norm.priority(data.get("priority", 2)),
        needs_reply=nr if isinstance(nr, bool) else str(nr).lower() in ("true", "yes", "1", "예"),
        action_items=actions,
        mentioned_events=events,
        by_llm=True,
    )


_ACTION_WORDS = re.compile(r"(요청|부탁|회신|검토|제출|확인 바랍|please|could you|action required|by EOD|until)", re.I)
_NEWSLETTER = re.compile(r"(newsletter|unsubscribe|수신거부|광고|뉴스레터)", re.I)


def heuristic_insight(mail: Email, max_chars: int, categories: dict[str, str], fail_code: str = "") -> MailInsight:
    body = clean_body(mail.body_text, max_chars)
    text = f"{mail.subject}\n{body}"
    if mail.is_meeting_request:
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
    actions = []
    if mail.flag_due:
        actions.append(ActionItem(f"[플래그] {mail.subject}", mail.flag_due, "me", mail.id, mail.subject))
    return MailInsight(
        email_id=mail.id,
        summary=first or mail.subject,
        category=cat,
        priority=1 if mail.importance == "high" else (2 if cat == "action_required" else 3),
        needs_reply=cat == "action_required",
        action_items=actions,
        by_llm=False,
        fail_code=fail_code,
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


class Extractor:
    """Thread-safe if `store` is None (pipeline writes the cache in the main thread)."""

    def __init__(self, llm, store, model_name: str, me: str, tz: str, max_chars: int,
                 categories: dict[str, str], aliases: dict[str, str] | None = None,
                 retry_chars: int = 1500, min_body_chars: int = 5, debug_dir: Path | None = None) -> None:
        self.llm, self.store = llm, store
        self.cache_key = f"{model_name}|{PROMPT_VERSION}|{'/'.join(categories)}"
        self.me, self.tzname, self.tz, self.max_chars = me, tz, ZoneInfo(tz), max_chars
        self.categories, self.aliases = categories, aliases or {}
        self.retry_chars, self.min_body_chars, self.debug_dir = retry_chars, min_body_chars, debug_dir
        self.system = extract_system(categories, me, tz)
        self.llm_calls = 0
        self.failures: list[Failure] = []
        self.warnings: dict[str, list[str]] = {}

    def user_prompt(self, mail: Email, max_chars: int) -> str:
        return EXTRACT_USER.format(
            received=mail.received.isoformat(),
            sender=f"{mail.sender_name} <{mail.sender}>",
            to=", ".join(mail.to[:10]),
            cc=", ".join(mail.cc[:10]),
            subject=mail.subject,
            importance=mail.importance,
            attachments="yes" if mail.has_attachments else "no",
            body=clean_body(mail.body_text, max_chars) or "(empty body)",
        )

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

    def analyze(self, mail: Email) -> MailInsight:
        if self.store:
            cached = self.store.get_insight(mail.id, self.cache_key)
            if cached:
                return cached
        body = clean_body(mail.body_text, self.max_chars)
        if self.llm is None or (len(body) < self.min_body_chars and not mail.subject.strip()):
            return heuristic_insight(mail, self.max_chars, self.categories)

        norm = Normalizer(self.categories, self.aliases)
        raw = ""
        last: LLMError | None = None
        for chars in (self.max_chars, self.retry_chars):
            user = self.user_prompt(mail, chars)
            try:
                data, raw = self._call(user, last_try=chars == self.retry_chars)
                ins = insight_from_json(data, mail, self.tz, norm)
                if data.get("_repaired_truncation"):
                    norm.warnings.append("output truncation repaired")
                if norm.warnings:
                    self.warnings[mail.id] = norm.warnings
                if self.store:
                    self.store.put_insight(ins, self.cache_key)
                return ins
            except LLMError as e:
                last, raw = e, e.raw or raw
                if e.code not in _RETRY_SHORTER or chars <= self.retry_chars:
                    break
                log.info("retry %s with shorter body (%s)", mail.id, e.code)
        f = Failure(mail.id, mail.subject, last.code, str(last)[:300], len(body), raw[:2000], norm.warnings)
        self.failures.append(f)
        log.warning("LLM extraction failed for %s [%s] %s -> rule fallback", mail.id, f.code, f.message[:120])
        if self.debug_dir:
            self._dump(mail, f)
        return heuristic_insight(mail, self.max_chars, self.categories, fail_code=last.code)

    def _dump(self, mail: Email, f: Failure) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", mail.id)[-60:]
        (self.debug_dir / f"{safe}.txt").write_text(
            f"CODE: {f.code}\nMESSAGE: {f.message}\nWARNINGS: {f.warnings}\n\n=== SYSTEM ===\n{self.system}\n"
            f"=== USER ===\n{self.user_prompt(mail, self.max_chars)}\n=== RAW OUTPUT ===\n{f.raw}\n",
            encoding="utf-8")
