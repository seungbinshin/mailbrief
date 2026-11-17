"""Outlook desktop (Classic) via COM on Windows. Uses pywin32.

Use this source when the company blocks Graph app registration and EWS access.
It reads the mailbox that the running Outlook profile has open, so no extra credentials are needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..models import (RESP_ACCEPTED, RESP_DECLINED, RESP_NONE, RESP_NOT_RESPONDED, RESP_ORGANIZER,
                      RESP_TENTATIVE, Email, Event)

OL_SENT, OL_INBOX, OL_CALENDAR = 5, 6, 9
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
OL_MAIL, OL_MEETING_REQUEST = 43, 53
# OlResponseStatus enumeration
_RESP = {0: RESP_NONE, 1: RESP_ORGANIZER, 2: RESP_TENTATIVE, 3: RESP_ACCEPTED, 4: RESP_DECLINED,
         5: RESP_NOT_RESPONDED}
_IMPORTANCE = {0: "low", 1: "normal", 2: "high"}
_NO_DATE = datetime(4501, 1, 1)


def _filter_dt(d: datetime) -> str:
    # Restrict() wants local time in "MM/dd/yyyy hh:mm AM" format.
    return d.astimezone().strftime("%m/%d/%Y %I:%M %p")


class OutlookCOMSource:
    def __init__(self, cfg, tz) -> None:
        import win32com.client  # optional dependency: pip install mailbrief[com]

        self.cfg, self.tz = cfg, tz
        self.app = win32com.client.Dispatch("Outlook.Application")
        self.ns = self.app.GetNamespace("MAPI")

    def _aware(self, d) -> datetime:
        d = datetime(d.year, d.month, d.day, d.hour, d.minute, d.second)
        return d.replace(tzinfo=self.tz)  # pywin32 returns local wall time

    def _smtp(self, entry, fallback: str) -> str:
        try:
            if entry.Type == "EX":  # Exchange DN -> SMTP
                u = entry.GetExchangeUser()
                return u.PrimarySmtpAddress if u else fallback
        except Exception:
            pass
        return fallback

    def _to_email(self, it, folder: str) -> Email:
        try:
            sender = it.SenderEmailAddress
            if it.SenderEmailType == "EX":
                sender = self._smtp(it.Sender, sender)
        except Exception:
            sender = ""
        if folder == "sent" and not sender:
            sender = self.cfg.my_address
        to, cc = [], []
        for r in it.Recipients:
            addr = r.Address
            try:
                addr = self._smtp(r.AddressEntry, addr)
            except Exception:
                pass
            (to if r.Type == 1 else cc).append(addr)
        due = None
        try:
            if it.IsMarkedAsTask and it.TaskDueDate and it.TaskDueDate.year < 4500:
                due = self._aware(it.TaskDueDate).replace(hour=18, minute=0)
        except Exception:
            pass
        mid = ""
        try:
            mid = it.PropertyAccessor.GetProperty(PR_INTERNET_MESSAGE_ID) or ""
        except Exception:
            pass
        when = it.SentOn if folder == "sent" else it.ReceivedTime
        return Email(
            id=it.EntryID, subject=it.Subject or "", sender=sender, sender_name=it.SenderName or "",
            received=self._aware(when), body_text=it.Body or "", to=to, cc=cc,
            is_read=folder == "sent" or not it.UnRead, importance=_IMPORTANCE.get(it.Importance, "normal"),
            conversation_id=getattr(it, "ConversationID", "") or "",
            has_attachments=it.Attachments.Count > 0, flag_due=due,
            is_meeting_request=it.Class == OL_MEETING_REQUEST, message_id=str(mid), folder=folder,
        )

    def _fetch(self, folder_id: int, field: str, folder: str, since: datetime, limit: int) -> list[Email]:
        items = self.ns.GetDefaultFolder(folder_id).Items
        items.Sort(f"[{field}]", True)
        items = items.Restrict(f"[{field}] >= '{_filter_dt(since)}'")
        out = []
        for it in items:
            if len(out) >= limit:
                break
            if it.Class not in (OL_MAIL, OL_MEETING_REQUEST):
                continue
            out.append(self._to_email(it, folder))
        return out

    def fetch_mails(self, since: datetime, limit: int) -> list[Email]:
        return self._fetch(OL_INBOX, "ReceivedTime", "inbox", since, limit)

    def fetch_sent(self, since: datetime, limit: int) -> list[Email]:
        return self._fetch(OL_SENT, "SentOn", "sent", since, limit)

    def fetch_events(self, start: datetime, end: datetime) -> list[Event]:
        items = self.ns.GetDefaultFolder(OL_CALENDAR).Items
        items.IncludeRecurrences = True  # must be set before Sort and Restrict
        items.Sort("[Start]")
        items = items.Restrict(f"[Start] <= '{_filter_dt(end)}' AND [End] >= '{_filter_dt(start)}'")
        out = []
        for it in items:
            s = self._aware(it.Start)
            if s > end + timedelta(days=1):
                break
            status = _RESP.get(it.ResponseStatus, RESP_NONE)
            out.append(Event(
                id=f"{it.EntryID}:{s.isoformat()}", subject=it.Subject or "", start=s, end=self._aware(it.End),
                location=it.Location or "", organizer=it.Organizer or "",
                is_organizer=status == RESP_ORGANIZER, response_status=status,
                response_requested=bool(it.ResponseRequested), is_cancelled=it.MeetingStatus in (5, 7),
                is_all_day=bool(it.AllDayEvent), attendee_count=it.Recipients.Count,
            ))
        return out

    def send_to_self(self, subject: str, html_body: str) -> None:
        m = self.app.CreateItem(0)
        m.To = self.cfg.my_address or self.ns.CurrentUser.AddressEntry.GetExchangeUser().PrimarySmtpAddress
        m.Subject, m.HTMLBody = subject, html_body
        m.Send()
