"""Microsoft Graph source (Exchange Online / Microsoft 365).

Needs an Entra ID app registration (public client, device code flow) with delegated
permissions: Mail.Read, Calendars.Read, and Mail.Send if send_to_self is used.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..config import expand
from ..models import (RESP_ACCEPTED, RESP_DECLINED, RESP_NONE, RESP_NOT_RESPONDED, RESP_ORGANIZER,
                      RESP_TENTATIVE, Email, Event)

GRAPH = "https://graph.microsoft.com/v1.0"
_RESP = {
    "none": RESP_NONE, "organizer": RESP_ORGANIZER, "tentativelyAccepted": RESP_TENTATIVE,
    "accepted": RESP_ACCEPTED, "declined": RESP_DECLINED, "notResponded": RESP_NOT_RESPONDED,
}


class GraphSource:
    def __init__(self, cfg, tz) -> None:
        import msal  # optional dependency: pip install mailbrief[graph]

        self.cfg, self.tz = cfg, tz
        self.scopes = ["Mail.Read", "Calendars.Read"] + (["Mail.Send"] if cfg.graph_send_scope else [])
        self.cache_path = expand(cfg.token_cache)
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        self.app = msal.PublicClientApplication(
            cfg.client_id, authority=f"https://login.microsoftonline.com/{cfg.tenant_id}", token_cache=self.cache
        )

    # ---- auth ----
    def _token(self) -> str:
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(self.scopes, account=accounts[0]) if accounts else None
        if not result:
            flow = self.app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise RuntimeError(f"Device flow failed: {flow}")
            print(flow["message"], flush=True)
            result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Token error: {result.get('error_description')}")
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self.cache.serialize())
        return result["access_token"]

    def _get(self, url: str, headers: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
            return json.loads(r.read().decode())

    def _paged(self, url: str, headers: dict, limit: int):
        n = 0
        while url and n < limit:
            data = self._get(url, headers)
            for item in data.get("value", []):
                yield item
                n += 1
                if n >= limit:
                    return
            url = data.get("@odata.nextLink")

    # ---- mail ----
    def _to_email(self, m: dict, folder: str) -> Email:
        frm = (m.get("from") or {}).get("emailAddress", {})
        due = ((m.get("flag") or {}).get("dueDateTime") or {}).get("dateTime")
        when = m.get("sentDateTime") if folder == "sent" else m.get("receivedDateTime")
        return Email(
            id=m["id"],
            subject=m.get("subject") or "",
            sender=frm.get("address", "") or (self.cfg.my_address if folder == "sent" else ""),
            sender_name=frm.get("name", ""),
            received=_parse(when or m["receivedDateTime"], self.tz),
            body_text=(m.get("body") or {}).get("content", ""),
            to=[r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
            cc=[r["emailAddress"]["address"] for r in m.get("ccRecipients", [])],
            is_read=folder == "sent" or m.get("isRead", False),
            importance=m.get("importance", "normal"),
            conversation_id=m.get("conversationId", ""),
            has_attachments=m.get("hasAttachments", False),
            flag_due=_parse(due, self.tz) if due else None,
            is_meeting_request=m.get("meetingMessageType") == "meetingRequest",
            message_id=m.get("internetMessageId", "") or "",
            folder=folder,
        )

    def _fetch(self, folder: str, field: str, kind: str, since: datetime, limit: int) -> list[Email]:
        q = urllib.parse.urlencode({
            "$filter": f"{field} ge {since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": f"{field} desc",
            "$top": "50",
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,body,isRead,"
                       "importance,conversationId,hasAttachments,flag,meetingMessageType,internetMessageId",
        })
        headers = {"Prefer": f'outlook.body-content-type="text", outlook.timezone="{self.tz.key}"'}
        return [self._to_email(m, kind)
                for m in self._paged(f"{GRAPH}/me/mailFolders/{folder}/messages?{q}", headers, limit)]

    def fetch_mails(self, since: datetime, limit: int) -> list[Email]:
        return self._fetch("inbox", "receivedDateTime", "inbox", since, limit)

    def fetch_sent(self, since: datetime, limit: int) -> list[Email]:
        return self._fetch("sentitems", "sentDateTime", "sent", since, limit)

    # ---- calendar ----
    def fetch_events(self, start: datetime, end: datetime) -> list[Event]:
        q = urllib.parse.urlencode({
            "startDateTime": start.isoformat(), "endDateTime": end.isoformat(),
            "$orderby": "start/dateTime", "$top": "100",
            "$select": "id,subject,start,end,location,organizer,isOrganizer,responseStatus,responseRequested,"
                       "isCancelled,isAllDay,onlineMeeting,attendees",
        })
        headers = {"Prefer": f'outlook.timezone="{self.tz.key}"'}
        out = []
        for e in self._paged(f"{GRAPH}/me/calendarView?{q}", headers, 500):
            out.append(Event(
                id=e["id"],
                subject=e.get("subject") or "",
                start=_parse(e["start"]["dateTime"], self.tz),
                end=_parse(e["end"]["dateTime"], self.tz),
                location=(e.get("location") or {}).get("displayName", ""),
                organizer=((e.get("organizer") or {}).get("emailAddress") or {}).get("address", ""),
                is_organizer=e.get("isOrganizer", False),
                response_status=_RESP.get((e.get("responseStatus") or {}).get("response", "none"), RESP_NONE),
                response_requested=e.get("responseRequested", True),
                is_cancelled=e.get("isCancelled", False),
                is_all_day=e.get("isAllDay", False),
                online_url=(e.get("onlineMeeting") or {}).get("joinUrl", "") if e.get("onlineMeeting") else "",
                attendee_count=len(e.get("attendees", [])),
            ))
        return out

    def send_to_self(self, subject: str, html_body: str) -> None:
        me = self.cfg.my_address
        body = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": html_body},
                            "toRecipients": [{"emailAddress": {"address": me}}]}, "saveToSentItems": False}
        req = urllib.request.Request(
            f"{GRAPH}/me/sendMail", json.dumps(body).encode(),
            {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=60).close()


def _parse(v: str, tz) -> datetime:
    v = v.replace("Z", "+00:00")
    if "." in v:  # Graph sends 7 fractional digits; fromisoformat accepts up to 6
        head, frac = v.split(".", 1)
        tail = ""
        for sign in ("+", "-"):
            if sign in frac:
                frac, tail = frac.split(sign, 1)
                tail = sign + tail
                break
        v = f"{head}.{frac[:6]}{tail}"
    d = datetime.fromisoformat(v)
    return d.replace(tzinfo=tz) if d.tzinfo is None else d.astimezone(tz)
