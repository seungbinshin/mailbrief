"""JSON fixture source for tests and offline demo."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..models import Email, Event


def _dt(v):
    return datetime.fromisoformat(v) if v else None


class FixtureSource:
    def __init__(self, cfg, tz) -> None:
        self.dir = Path(cfg.fixture_dir)
        self.tz = tz
        self.sent: list[tuple[str, str]] = []

    def fetch_mails(self, since: datetime, limit: int) -> list[Email]:
        raw = json.loads((self.dir / "mails.json").read_text(encoding="utf-8"))
        out = []
        for m in raw:
            m = dict(m)
            m["received"] = _dt(m["received"])
            m["flag_due"] = _dt(m.get("flag_due"))
            e = Email(**m)
            if e.received >= since:
                out.append(e)
        return sorted(out, key=lambda e: e.received, reverse=True)[:limit]

    def fetch_events(self, start: datetime, end: datetime) -> list[Event]:
        raw = json.loads((self.dir / "events.json").read_text(encoding="utf-8"))
        out = []
        for ev in raw:
            ev = dict(ev)
            ev["start"], ev["end"] = _dt(ev["start"]), _dt(ev["end"])
            e = Event(**ev)
            if e.end >= start and e.start <= end:
                out.append(e)
        return sorted(out, key=lambda e: e.start)

    def send_to_self(self, subject: str, html_body: str) -> None:
        self.sent.append((subject, html_body))
