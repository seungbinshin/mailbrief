"""Data models shared by all modules."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

# Normalized response status values (all sources map to these).
RESP_NONE = "none"
RESP_ORGANIZER = "organizer"
RESP_TENTATIVE = "tentative"
RESP_ACCEPTED = "accepted"
RESP_DECLINED = "declined"
RESP_NOT_RESPONDED = "notResponded"



@dataclass
class Email:
    id: str
    subject: str
    sender: str
    received: datetime
    body_text: str
    sender_name: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    is_read: bool = False
    importance: str = "normal"  # low | normal | high
    conversation_id: str = ""
    has_attachments: bool = False
    flag_due: Optional[datetime] = None
    is_meeting_request: bool = False
    message_id: str = ""      # Internet Message-ID: removes copies of one mail (inbox + sent, two folders)
    folder: str = "inbox"     # inbox | sent


@dataclass
class Event:
    id: str
    subject: str
    start: datetime
    end: datetime
    location: str = ""
    organizer: str = ""
    is_organizer: bool = False
    response_status: str = RESP_NONE
    response_requested: bool = True
    is_cancelled: bool = False
    is_all_day: bool = False
    online_url: str = ""
    attendee_count: int = 0


@dataclass
class ActionItem:
    description: str
    due: Optional[datetime]
    owner: str  # "me" | "other" | "unknown"
    source_email_id: str
    source_subject: str = ""

    @property
    def key(self) -> str:
        norm = re.sub(r"\W+", "", self.description.lower())[:60]
        due = self.due.date().isoformat() if self.due else "nodue"
        return hashlib.sha1(f"{norm}|{due}".encode()).hexdigest()[:10]


@dataclass
class MentionedEvent:
    title: str
    start: datetime
    end: Optional[datetime]
    location: str
    source_email_id: str
    source_subject: str = ""


@dataclass
class StoryVerdict:
    """Facts that the LLM extracts for the story decision. The decision itself is made by rules (stories.judge).

    A story is one goal that needs tracking over time. All three conditions must be present:
    persistence (work continues after the mail), owner (person or team), end_condition (a sentence that says
    when the work is done). An empty string means "not known".
    """
    persistence: bool = False
    owner: str = ""
    end_condition: str = ""
    title: str = ""
    goal: str = ""
    confidence: float = 0.0   # high = 0.9, medium = 0.6, low = 0.3
    done: bool = False        # the latest message says that the end condition is met
    match_id: str = ""        # open story that the LLM says this thread belongs to (e.g. "S3")
    reason: str = ""


@dataclass
class MailInsight:
    """Analysis of one thread (a single mail is a thread of one). email_id is the latest mail of the thread."""
    email_id: str
    summary: str
    category: str
    priority: int  # 1 = high, 2 = medium, 3 = low
    needs_reply: bool
    action_items: list[ActionItem] = field(default_factory=list)
    mentioned_events: list[MentionedEvent] = field(default_factory=list)
    by_llm: bool = True
    fail_code: str = ""   # set when the rule fallback was used because the LLM failed
    thread_key: str = ""
    email_ids: list[str] = field(default_factory=list)
    meeting_type: str = ""
    story: StoryVerdict = field(default_factory=StoryVerdict)

    def to_dict(self) -> dict:
        d = asdict(self)
        for a in d["action_items"]:
            a["due"] = a["due"].isoformat() if a["due"] else None
        for m in d["mentioned_events"]:
            m["start"] = m["start"].isoformat()
            m["end"] = m["end"].isoformat() if m["end"] else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MailInsight":
        d = dict(d)
        d["action_items"] = [
            ActionItem(**{**a, "due": datetime.fromisoformat(a["due"]) if a["due"] else None})
            for a in d.get("action_items", [])
        ]
        d["mentioned_events"] = [
            MentionedEvent(**{
                **m,
                "start": datetime.fromisoformat(m["start"]),
                "end": datetime.fromisoformat(m["end"]) if m["end"] else None,
            })
            for m in d.get("mentioned_events", [])
        ]
        st = d.get("story") or {}
        d["story"] = StoryVerdict(**{k: v for k, v in st.items() if k in StoryVerdict.__dataclass_fields__})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Thread:
    """Mails of one conversation, oldest first. Copies are removed (see threads.group_threads)."""
    key: str                       # primary key: "c:<conversation id>" or "s:<subject hash>"
    subject: str                   # subject without RE:/FW: prefixes
    mails: list[Email]
    keys: set[str] = field(default_factory=set)   # all keys of the merged groups (story links use any of them)

    @property
    def latest(self) -> Email:
        return self.mails[-1]

    @property
    def ids(self) -> list[str]:
        return [m.id for m in self.mails]

    @property
    def revision(self) -> str:
        """Cache key of the thread analysis. A new mail is the new latest mail, so it makes a new revision.
        Older mails that leave the fetch window (lookback, max_mails) do NOT change it: no new LLM call."""
        last = self.latest
        return hashlib.sha1((last.message_id.strip().strip("<>").lower() or last.id).encode()).hexdigest()[:16]
