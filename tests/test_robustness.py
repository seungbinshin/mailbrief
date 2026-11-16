import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import CANNED, FakeLLM

from mailbrief.cards import build_cards, load_cards, save_cards, to_cards_html
from mailbrief.extract import Extractor
from mailbrief.llm import E_BAD_JSON, E_EMPTY, E_NO_JSON, LLMError, parse_json
from mailbrief.models import Email
from mailbrief.pipeline import make_extractor, run
from mailbrief.store import Store
from mailbrief.watch import watch

TZ = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 17, 8, 30, tzinfo=TZ)
MAIL = Email(id="x1", subject="리포트 요청", sender="a@corp.com", received=NOW, body_text="본문 " * 400, to=["me@corp.com"])


# ---------- JSON parsing ----------
def test_parse_json_repairs_common_model_mistakes():
    assert parse_json('<think>let me think {"no": 1}</think>{"a": 1}') == {"a": 1}
    assert parse_json('{"a": [1, 2,], "b": True, "c": None}') == {"a": [1, 2], "b": True, "c": None}
    assert parse_json('{"a": 1, // comment\n "b": 2}') == {"a": 1, "b": 2}
    out = parse_json('{"summary": "x", "action_items": [{"description": "y", "due', allow_truncated=True)
    assert out["summary"] == "x" and out["_repaired_truncation"]
    for bad, code in (("", E_EMPTY), ("죄송합니다. 요약할 수 없습니다.", E_NO_JSON), ('{"a": 1 "b"}', E_BAD_JSON)):
        with pytest.raises(LLMError) as e:
            parse_json(bad)
        assert e.value.code == code


# ---------- normalization ----------
class OneShot:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.users = []

    def chat_ex(self, system, user, json_output=True):
        self.users.append(user)
        return self.outputs.pop(0)


def _ex(llm, **kw):
    from mailbrief.config import DEFAULT_CATEGORIES
    return Extractor(llm, None, "m", "me@corp.com", "Asia/Seoul", 4000, kw.get("cats", dict(DEFAULT_CATEGORIES)),
                     kw.get("aliases"), retry_chars=500)


def test_normalizes_labels_priority_and_dates():
    out = json.dumps({"summary": "s", "category": "조치 필요", "priority": "high", "needs_reply": "yes",
                      "action_items": [{"description": "a", "due": "2026/11/18 17:00", "owner": "me"},
                                       {"description": "b", "due": "다음주쯤", "owner": "boss"}],
                      "events": {"title": "t", "start": "2026-11-19 14:00"}}, ensure_ascii=False)
    ex = _ex(OneShot((out, "stop")))
    ins = ex.analyze(MAIL)
    assert (ins.category, ins.priority, ins.needs_reply) == ("action_required", 1, True)
    assert ins.action_items[0].due == datetime(2026, 11, 18, 17, 0, tzinfo=TZ)
    assert ins.action_items[1].due is None and ins.action_items[1].owner == "unknown"
    assert ins.mentioned_events[0].start.hour == 14
    assert any("bad due" in w for w in ex.warnings["x1"])


def test_truncated_output_retries_with_shorter_body():
    good = json.dumps(CANNED["Q4 MPE"], ensure_ascii=False)
    llm = OneShot(('{"summary": "잘린', "length"), (good, "stop"))
    ex = _ex(llm)
    ins = ex.analyze(MAIL)
    assert ins.by_llm and ex.llm_calls == 2 and not ex.failures
    assert len(llm.users[1]) < len(llm.users[0])  # second prompt has the shorter body


def test_schema_mismatch_falls_back_with_code():
    ex = _ex(OneShot(('{"요약": "x", "분류": "참고"}', "stop")))
    ins = ex.analyze(MAIL)
    assert not ins.by_llm and ins.fail_code == "E_SCHEMA" and ex.failures[0].code == "E_SCHEMA"


def test_custom_categories_and_aliases():
    cats = {"urgent": "오늘 처리", "later": "나중에", "ignore": "무시"}
    ex = _ex(OneShot(('{"summary": "s", "category": "긴급", "priority": 1}', "stop")), cats=cats,
             aliases={"긴급": "urgent"})
    assert ex.analyze(MAIL).category == "urgent"
    assert '"urgent" | "later" | "ignore"' in ex.system and "- later: 나중에" in ex.system


# ---------- read mails, watch, cards ----------
def test_read_mails_are_processed(cfg):
    rep = run(cfg, now=NOW, llm=FakeLLM(), deliver=False)
    m8 = next(d for d in rep.digest if d.mail.id == "m8")
    assert m8.mail.is_read is True and m8.insight.by_llm


def test_watch_notifies_once(cfg):
    cfg.watch.poll_minutes = 24 * 60
    store = Store(cfg.output.db_path)
    from mailbrief.sources.fixture import FixtureSource
    src = FixtureSource(cfg.source, TZ)
    hits = watch(cfg, once=True, popup=False, llm=FakeLLM(), source=src, store=store, clock=lambda: NOW)
    assert {(m.id, r) for m, _, r in hits} == {("m1", "중요"), ("m6", "회의 초대"), ("m8", "마감 11/17 18:00")}
    again = watch(cfg, once=True, popup=False, llm=FakeLLM(), source=src, store=store, clock=lambda: NOW)
    assert again == []


def test_cards(cfg, tmp_path):
    rep = run(cfg, now=NOW, llm=FakeLLM(), deliver=False)
    cards = build_cards(rep)
    assert [c.title for c in cards] == ["오늘의 핵심", "놓치고 있는 일", "응답이 필요한 회의 초대", "다가오는 일정",
                                        "마감 · 캘린더 미등록 일정", "주요 메일", "그 밖의 메일"]
    assert dict(cards[0].stats)["놓친 일"] == 4
    p = save_cards(cards, tmp_path / "c.json", "t", "")
    again, title, _ = load_cards(p)
    assert title == "t" and again[1].items[0].text == cards[1].items[0].text
    page = to_cards_html(rep)
    assert "const CARDS=" in page and "놓치고 있는 일" in page and "</script>" in page


def test_diagnose_lists_failures(cfg, capsys):
    run(cfg, now=NOW, llm=FakeLLM(fail_on="Q4 MPE"), deliver=False)
    from mailbrief.diagnose import diagnose
    diagnose(cfg)
    out = capsys.readouterr().out
    assert "[E_TRANSPORT] 1건" in out and "Q4 MPE" in out


def test_cache_key_changes_with_categories(cfg):
    k1 = make_extractor(cfg, None, None).cache_key
    cfg.analysis.categories = {"a": "x", "b": "y"}
    assert make_extractor(cfg, None, None).cache_key != k1


def test_truncated_twice_uses_repaired_partial_output():
    cut = '{"summary": "부분 요약", "category": "fyi", "priority": 3, "action_items": [{"description": "x", "due'
    ex = _ex(OneShot((cut, "length"), (cut, "length")))
    ins = ex.analyze(MAIL)
    assert ins.by_llm and ins.summary == "부분 요약"
    assert "output truncation repaired" in ex.warnings["x1"]
