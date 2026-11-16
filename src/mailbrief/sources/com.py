"""Outlook desktop (Classic) via COM on Windows. Uses pywin32.

Use this source when the company blocks Graph app registration and EWS access.
It reads the mailbox that the running Outlook profile has open, so no extra credentials are needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..models import (RESP_ACCEPTED, RESP_DECLINED, RESP_NONE, RESP_NOT_RESPONDED, RESP_ORGANIZER,
                      RESP_TENTATIVE, Email, Event)

OL_INBOX, OL_CALENDAR = 6, 9
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

    def fetch_mails(self, since: datetime, limit: int) -> list[Email]:
        items = self.ns.GetDefaultFolder(OL_INBOX).Items
        items.Sort("[ReceivedTime]", True)
        items = items.Restrict(f"[ReceivedTime] >= '{_filter_dt(since)}'")
        out = []
        for it in items:
            if len(out) >= limit:
                break
            if it.Class not in (OL_MAIL, OL_MEETING_REQUEST):
                continue
            try:
                sender = it.SenderEmailAddress
                if it.SenderEmailType == "EX":  # Exchange DN -> SMTP
                    ex = it.Sender.GetExchangeUser()
                    sender = ex.PrimarySmtpAddress if ex else sender
            except Exception:
                sender = ""
            to, cc = [], []
            for r in it.Recipients:
                addr = r.Address
                try:
                    if r.AddressEntry.Type == "EX":
                        u = r.AddressEntry.GetExchangeUser()
                        addr = u.PrimarySmtpAddress if u else addr
                except Exception:
                    pass
                (to if r.Type == 1 else cc).append(addr)
            due = None
            try:
                if it.IsMarkedAsTask and it.TaskDueDate and it.TaskDueDate.year < 4500:
                    due = self._aware(it.TaskDueDate).replace(hour=18, minute=0)
            except Exception:
                pass
            out.append(Email(
                id=it.EntryID, subject=it.Subject or "", sender=sender, sender_name=it.SenderName or "",
                received=self._aware(it.ReceivedTime), body_text=it.Body or "", to=to, cc=cc,
                is_read=not it.UnRead, importance=_IMPORTANCE.get(it.Importance, "normal"),
                conversation_id=getattr(it, "ConversationID", "") or "",
                has_attachments=it.Attachments.Count > 0, flag_due=due,
                is_meeting_request=it.Class == OL_MEETING_REQUEST,
            ))
        return out

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
