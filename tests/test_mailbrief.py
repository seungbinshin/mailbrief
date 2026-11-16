from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import FakeLLM

from mailbrief.config import load_config
from mailbrief.llm import parse_json
from mailbrief.pipeline import run
from mailbrief.preprocess import clean_body
from mailbrief.report import to_markdown
from mailbrief.store import Store

NOW = datetime(2026, 11, 17, 8, 30, tzinfo=ZoneInfo("Asia/Seoul"))


def kinds(findings):
    return sorted((f.kind, f.ref_id) for f in findings)


def test_clean_body_strips_html_quote_signature():
    raw = "<p>안녕하세요</p><p>요청 드립니다.</p><p>감사합니다.</p><p>-----Original Message-----</p><p>From: x</p><p>old</p>"
    out = clean_body(raw)
    assert "요청 드립니다." in out and "old" not in out and "Original" not in out
    assert clean_body("본문\n> 인용\n> 인용2") == "본문"
    assert clean_body("a" * 50, max_chars=10).endswith("[truncated]")


def test_parse_json_accepts_fences_and_noise():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here: {"a": {"b": 2}} done') == {"a": {"b": 2}}


def test_pipeline_with_llm(cfg):
    llm = FakeLLM()
    rep = run(cfg, now=NOW, llm=llm)

    # noreply sender is skipped; digest = mails in last 24h (m1, m3, m4, m6, m8)
    assert {d.mail.id for d in rep.digest} == {"m1", "m3", "m4", "m6", "m8"}
    assert rep.digest[0].mail.id == "m1"  # priority 1 first

    assert kinds(rep.missed) == sorted([
        ("unanswered_invite", "e1"),       # passed meeting without response
        ("overdue_action", "m2"),          # due 11/14
        ("overdue_action", "m5"),          # training due 11/15
        ("missed_mail_event", "m5"),       # training not on calendar
    ])
    m2 = next(f for f in rep.missed if f.ref_id == "m2")
    assert m2.detail.endswith("회신 필요")          # reply + overdue action merged into one entry
    assert kinds(rep.attention) == [("unanswered_invite", "e3")]
    assert [e.id for e in rep.upcoming_events] == ["e2", "e3", "e5"]  # declined + cancelled removed
    assert kinds(rep.upcoming) == sorted([
        ("upcoming_deadline", "m8"),       # Outlook flag due today 18:00
        ("upcoming_deadline", "m1"),
        ("mail_event_not_on_calendar", "m3"),
    ])
    # Weekly sync from mail m6 matches calendar e3 -> not reported as missing
    assert all(f.ref_id != "m6" for f in rep.upcoming + rep.missed)
    assert rep.briefing.startswith("- ")

    from pathlib import Path
    names = sorted(p.name for p in Path(cfg.output.dir).iterdir())
    assert names == ["brief_20261117_0830.html", "brief_20261117_0830.md", "latest_cards.html", "latest_cards.json"]


def test_cache_prevents_second_llm_call(cfg):
    llm = FakeLLM()
    run(cfg, now=NOW, llm=llm, deliver=False)
    first = llm.calls
    run(cfg, now=NOW, llm=llm, deliver=False)
    assert llm.calls - first == 1  # only the briefing call


def test_done_key_hides_action(cfg):
    rep = run(cfg, now=NOW, llm=FakeLLM(), deliver=False)
    key = next(f.key for f in rep.missed if f.kind == "overdue_action" and f.ref_id == "m2")
    Store(cfg.output.db_path).set_action_status(key, "done")
    rep2 = run(cfg, now=NOW, llm=FakeLLM(), deliver=False)
    assert ("overdue_action", "m2") not in kinds(rep2.missed)


def test_llm_failure_falls_back_to_rules(cfg):
    rep = run(cfg, now=NOW, llm=FakeLLM(fail_on="Q4 MPE", fenced=True), deliver=False)
    m1 = next(d for d in rep.digest if d.mail.id == "m1")
    assert m1.insight.by_llm is False and m1.insight.category == "action_required"
    assert rep.stats["llm_errors"] == 1
    from mailbrief.pipeline import make_extractor
    key = make_extractor(cfg, None, None).cache_key
    assert Store(cfg.output.db_path).get_insight("m1", key) is None  # retried next run
    assert Store(cfg.output.db_path).get_insight("m3", key) is not None
    assert [f["code"] for f in Store(cfg.output.db_path).failures()] == ["E_TRANSPORT"]


def test_no_llm_mode(cfg):
    rep = run(cfg, now=NOW, use_llm=False, deliver=False)
    assert ("unanswered_invite", "e1") in kinds(rep.missed)
    assert "메일 브리핑" in to_markdown(rep)


def test_send_to_self(cfg):
    from mailbrief.sources.fixture import FixtureSource
    cfg.output.send_to_self = True
    src = FixtureSource(cfg.source, ZoneInfo("Asia/Seoul"))
    run(cfg, now=NOW, llm=FakeLLM(), source=src)
    assert len(src.sent) == 1 and "놓친 일 4건" in src.sent[0][0]


def test_example_config_loads():
    from pathlib import Path
    c = load_config(Path(__file__).parents[1] / "config.example.toml")
    assert c.llm.base_url.startswith("http") and c.source.type in ("graph", "ews", "com", "fixture")


def test_overdue_reply_without_action(cfg):
    from mailbrief.models import MailInsight
    from mailbrief.analyze import build_report
    from mailbrief.sources.fixture import FixtureSource
    src = FixtureSource(cfg.source, ZoneInfo("Asia/Seoul"))
    mails = src.fetch_mails(datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Seoul")), 50)
    ins = {"m2": MailInsight("m2", "회신 요청", "action_required", 2, True)}
    rep = build_report(NOW, mails, ins, [], cfg.analysis, "me@corp.com", set())
    assert kinds(rep.missed) == [("overdue_reply", "m2")]
