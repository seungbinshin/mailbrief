from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Email, Event


class MailSource(ABC):
    """Read-only access to mail and calendar, plus optional send-to-self for the report."""

    @abstractmethod
    def fetch_mails(self, since: datetime, limit: int) -> list[Email]: ...

    @abstractmethod
    def fetch_events(self, start: datetime, end: datetime) -> list[Event]: ...

    def fetch_sent(self, since: datetime, limit: int) -> list[Email]:
        """Mails that I sent (folder="sent"). They complete the threads: my replies show who has the ball."""
        return []

    def send_to_self(self, subject: str, html_body: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support sending")


def make_source(cfg, tz) -> MailSource:
    t = cfg.type.lower()
    if t == "graph":
        from .graph import GraphSource
        return GraphSource(cfg, tz)
    if t == "ews":
        from .ews import EWSSource
        return EWSSource(cfg, tz)
    if t == "com":
        from .com import OutlookCOMSource
        return OutlookCOMSource(cfg, tz)
    if t == "fixture":
        from .fixture import FixtureSource
        return FixtureSource(cfg, tz)
    raise ValueError(f"Unknown source type: {cfg.type}")
