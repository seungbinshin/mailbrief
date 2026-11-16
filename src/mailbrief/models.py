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
class MailInsight:
    email_id: str
    summary: str
    category: str
    priority: int  # 1 = high, 2 = medium, 3 = low
    needs_reply: bool
    action_items: list[ActionItem] = field(default_factory=list)
    mentioned_events: list[MentionedEvent] = field(default_factory=list)
    by_llm: bool = True
    fail_code: str = ""   # set when the rule fallback was used because the LLM failed

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
        return cls(**d)
