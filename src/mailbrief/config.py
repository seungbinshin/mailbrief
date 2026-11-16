"""Configuration loader (TOML, stdlib only)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMConfig:
    base_url: str = "http://localhost:8000/v1"  # OpenAI-compatible endpoint (vLLM, TGI, Ollama ...)
    model: str = "in-house-llm"
    api_key_env: str = "MAILBRIEF_LLM_API_KEY"
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_s: int = 60
    json_mode: bool = True  # send response_format={"type":"json_object"}
    max_retries: int = 2
    verify_tls: bool = True
    ca_bundle: str = ""
    system_in_user: bool = False    # true for models whose chat template has no system role (e.g. Gemma)
    extra_body: dict = field(default_factory=dict)  # merged into the request, e.g. {chat_template_kwargs={enable_thinking=false}}

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class SourceConfig:
    type: str = "com"  # com (Windows Outlook) | ews | graph | fixture
    # graph
    tenant_id: str = ""
    client_id: str = ""
    token_cache: str = "~/.mailbrief/msal_cache.json"
    graph_send_scope: bool = False  # request Mail.Send (needed for output.send_to_self)
    # ews
    email: str = ""
    username: str = ""
    password_env: str = "MAILBRIEF_EWS_PASSWORD"
    ews_server: str = ""  # empty -> autodiscover
    # com
    outlook_profile: str = ""
    # fixture
    fixture_dir: str = "tests/fixtures"
    # common
    my_address: str = ""
    max_mails: int = 200


DEFAULT_CATEGORIES = {
    "action_required": "내가 해야 할 일(검토, 회신, 제출, 승인)이 있는 메일",
    "meeting": "회의 초대, 일정 변경, 회의록",
    "fyi": "참고용 공유. 내가 할 일 없음",
    "announcement": "전사/부서 공지, 교육, 제도 안내",
    "newsletter": "뉴스레터, 광고, 구독 메일",
    "other": "위 분류에 맞지 않는 메일",
}


@dataclass
class AnalysisConfig:
    timezone: str = "Asia/Seoul"
    digest_hours: int = 24          # mails in this window go to the daily digest
    lookback_days: int = 7          # window to detect missed items
    lookahead_days: int = 7         # window for upcoming items
    reply_overdue_hours: int = 48   # needs_reply mail older than this -> missed reply
    event_match_minutes: int = 30   # tolerance to match mail-mentioned event to calendar
    max_body_chars: int = 4000
    skip_senders: list[str] = field(default_factory=list)  # e.g. noreply@...
    # Classification. Keys are the labels the LLM must output; values are the rules shown to the LLM.
    categories: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CATEGORIES))
    # Extra labels the model may output -> canonical key (e.g. "요청" -> "action_required").
    category_aliases: dict[str, str] = field(default_factory=dict)
    retry_body_chars: int = 1500    # second try with a shorter body after E_CONTEXT / E_TRUNCATED / E_BAD_JSON
    min_body_chars: int = 5         # body shorter than this -> classify by rules (no LLM call)
    debug_dump: bool = False        # save prompt + raw output of failed mails under output.dir/debug


@dataclass
class OutputConfig:
    dir: str = "out"
    formats: list[str] = field(default_factory=lambda: ["md", "html"])
    send_to_self: bool = False
    db_path: str = "~/.mailbrief/state.sqlite3"
    popup: bool = True              # show the card popup after `run` (Windows desktop)
    open_html: bool = False         # open the HTML card page in the browser after `run`


@dataclass
class WatchConfig:
    poll_minutes: int = 5           # check for new mail every N minutes (all sources)
    use_com_events: bool = True     # COM source: react at once to Outlook NewMailEx event
    notify_priority: int = 1        # popup for new mail with priority <= this (1 = only high)
    notify_due_hours: int = 24      # popup for new mail with my action item due within N hours
    brief_times: list[str] = field(default_factory=lambda: ["08:30"])  # full briefing + card popup (HH:MM)


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)


def _fill(cls, data: dict):
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Unknown config keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**known)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    an = raw.get("analysis", {})
    if "categories" in an and not an["categories"]:
        raise ValueError("analysis.categories must not be empty")
    return Config(
        llm=_fill(LLMConfig, raw.get("llm", {})),
        source=_fill(SourceConfig, raw.get("source", {})),
        analysis=_fill(AnalysisConfig, raw.get("analysis", {})),
        output=_fill(OutputConfig, raw.get("output", {})),
        watch=_fill(WatchConfig, raw.get("watch", {})),
    )


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p))
