"""Exchange Web Services source (on-premise Exchange 2013+). Uses exchangelib."""
from __future__ import annotations

import os
from datetime import datetime

from ..models import (RESP_ACCEPTED, RESP_DECLINED, RESP_NONE, RESP_NOT_RESPONDED, RESP_ORGANIZER,
                      RESP_TENTATIVE, Email, Event)

_RESP = {
    "Unknown": RESP_NONE, "Organizer": RESP_ORGANIZER, "Tentative": RESP_TENTATIVE,
    "Accept": RESP_ACCEPTED, "Decline": RESP_DECLINED, "NoResponseReceived": RESP_NOT_RESPONDED,
}


class EWSSource:
    def __init__(self, cfg, tz) -> None:
        from exchangelib import DELEGATE, Account, Configuration, Credentials, EWSTimeZone

        self.cfg, self.tz = cfg, tz
        self.ews_tz = EWSTimeZone(tz.key)
        creds = Credentials(cfg.username or cfg.email, os.environ.get(cfg.password_env, ""))
        if cfg.ews_server:
            conf = Configuration(server=cfg.ews_server, credentials=creds)
            self.account = Account(cfg.email, config=conf, autodiscover=False, access_type=DELEGATE,
                                   default_timezone=self.ews_tz)
        else:
            self.account = Account(cfg.email, credentials=creds, autodiscover=True, access_type=DELEGATE,
                                   default_timezone=self.ews_tz)

    def fetch_mails(self, since: datetime, limit: int) -> list[Email]:
        from exchangelib import EWSDateTime

        qs = (self.account.inbox.filter(datetime_received__gte=EWSDateTime.from_datetime(since))
              .order_by("-datetime_received")
              .only("id", "subject", "sender", "to_recipients", "cc_recipients", "datetime_received",
                    "text_body", "body", "is_read", "importance", "conversation_id", "has_attachments",
                    "item_class"))
        out = []
        for m in qs[:limit]:
            out.append(Email(
                id=m.id,
                subject=m.subject or "",
                sender=m.sender.email_address if m.sender else "",
                sender_name=m.sender.name if m.sender else "",
                received=m.datetime_received.astimezone(self.tz),
                body_text=m.text_body or str(m.body or ""),
                to=[r.email_address for r in (m.to_recipients or [])],
                cc=[r.email_address for r in (m.cc_recipients or [])],
                is_read=bool(m.is_read),
                importance=(m.importance or "Normal").lower(),
                conversation_id=m.conversation_id.id if m.conversation_id else "",
                has_attachments=bool(m.has_attachments),
                is_meeting_request=(getattr(m, "item_class", "") or "").startswith("IPM.Schedule.Meeting.Request"),
            ))
        return out

    def fetch_events(self, start: datetime, end: datetime) -> list[Event]:
        from exchangelib import EWSDateTime

        items = self.account.calendar.view(start=EWSDateTime.from_datetime(start),
                                           end=EWSDateTime.from_datetime(end))
        out = []
        for e in items:
            s, en = e.start, e.end
            if not isinstance(s, datetime):  # all-day events come as EWSDate
                s = datetime(s.year, s.month, s.day, tzinfo=self.tz)
                en = datetime(en.year, en.month, en.day, tzinfo=self.tz)
            org = e.organizer.email_address if e.organizer else ""
            out.append(Event(
                id=e.id,
                subject=e.subject or "",
                start=s.astimezone(self.tz),
                end=en.astimezone(self.tz),
                location=e.location or "",
                organizer=org,
                is_organizer=org.lower() == (self.cfg.my_address or self.cfg.email).lower(),
                response_status=_RESP.get(e.my_response_type or "Unknown", RESP_NONE),
                response_requested=bool(e.is_response_requested) if e.is_response_requested is not None else True,
                is_cancelled=bool(e.is_cancelled),
                is_all_day=bool(e.is_all_day),
                attendee_count=len(e.required_attendees or []) + len(e.optional_attendees or []),
            ))
        return out

    def send_to_self(self, subject: str, html_body: str) -> None:
        from exchangelib import HTMLBody, Mailbox, Message

        Message(account=self.account, subject=subject, body=HTMLBody(html_body),
                to_recipients=[Mailbox(email_address=self.cfg.my_address or self.cfg.email)]).send(save_copy=False)
