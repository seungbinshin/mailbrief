"""Story tracking: each test repeats one problem measured in the in-house story tracker and checks the fix."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from story_fixture import ME, NOW, TZ, ListSource, StoryLLM, base_mails, mail, t

from mailbrief.config import Config, StoryConfig
from mailbrief.dashboard import export_folders, safe_name
from mailbrief.evaluate import check_source, format_score, predict_rows, read_csv, score, write_csv
from mailbrief.models import MailInsight, StoryVerdict
from mailbrief.pipeline import analyze_threads, fetch_threads, make_tracker, run
from mailbrief.store import Store
from mailbrief.stories import StoryTracker, judge
from mailbrief.story_cli import story_command
from mailbrief.threads import group_threads, normalize_subject


@pytest.fixture
def scfg(tmp_path):
    c = Config()
    c.source.type = "fixture"
    c.source.my_address = ME
    c.output.dir = str(tmp_path / "out")
    c.output.db_path = str(tmp_path / "state.sqlite3")
    c.output.popup = False
    c.stories.export_dir = str(tmp_path / "stories")
    return c


def _run(cfg, src, llm, now=NOW, deliver=False):
    return run(cfg, now=now, llm=llm, source=src, store=Store(cfg.output.db_path), deliver=deliver)


def _tracker(cfg):
    return StoryTracker(Store(cfg.output.db_path), cfg.stories, ME)


def _route_of(rep_threads_routes, subject_part, cfg, src):
    _, threads = fetch_threads(cfg, src, NOW - timedelta(days=7))
    return next(t for t in threads if subject_part in t.subject)


# ---------------------------------------------------------------- threads (RE: = same thread, 1 LLM call)

def test_normalize_subject():
    for s in ("RE: RE: 설계 리뷰", "FW: 설계 리뷰", "회신: 설계 리뷰", "전달: 설계 리뷰", "RE[2]: 설계 리뷰",
              "[EXT] Re: 설계  리뷰", "답장: FWD: 설계 리뷰"):
        assert normalize_subject(s) == "설계 리뷰", s


def test_re_chain_is_one_thread_and_one_llm_call(scfg):
    llm = StoryLLM()
    src = ListSource(base_mails())
    rep = _run(scfg, src, llm)
    # 14 mail objects: 1 copy removed, 3 + 3 mails in two threads -> 9 threads -> 9 extraction calls + 1 briefing
    assert rep.stats["threads"] == 9
    assert llm.calls == 9 + 1
    mpe = next(d for d in rep.digest if "Q4 MPE" in d.mail.subject)
    assert mpe.count == 3 and mpe.mail.id == "s3"           # thread with my sent reply, shown once
    _, threads = fetch_threads(scfg, src, NOW - timedelta(days=7))
    assert sorted(len(x.mails) for x in threads if "주간 보고" in x.subject) == [3]
    assert [x.ids for x in threads if x.subject == "팀 회의록 공유"] == [["c1"]]   # inbox copy kept


def test_subject_join_needs_shared_participant_and_gap():
    a = mail("x1", "문의", "a@corp.com", t(10, 9), "1", to=("me@corp.com",))
    b = mail("x2", "RE: 문의", "b@other.com", t(11, 9), "2", to=("c@other.com",))      # no shared participant
    c = mail("x3", "RE: 문의", "a@corp.com", datetime(2026, 12, 20, 9, tzinfo=TZ), "3")  # same person, 40 days later
    threads = group_threads([a, b, c], ME, gap_days=14)
    assert len(threads) == 3 and len({x.key for x in threads}) == 3


def test_sent_only_thread_is_not_analyzed():
    s = mail("o1", "제안서 송부", ME, t(15, 9), "제안서입니다.", to=("cust@client.com",), conv="cv", folder="sent")
    assert group_threads([s], ME) == []


def test_my_reply_ends_needs_reply(scfg):
    q1 = mail("q1", "설계 리뷰 자료 회신 부탁", "lee@corp.com", t(12, 9), "의견 부탁드립니다.", conv="cq")
    q2 = mail("q2", "RE: 설계 리뷰 자료 회신 부탁", ME, t(13, 7), "의견 드립니다.", to=("lee@corp.com",), conv="cq",
              folder="sent")
    scfg.analysis.reply_overdue_hours = 1
    rep = _run(scfg, ListSource([q1]), StoryLLM(), now=t(13, 8))                 # without my reply
    assert rep.digest[0].insight.needs_reply is True and "overdue_reply" in [f.kind for f in rep.missed]
    rep = _run(scfg, ListSource([q1, q2]), StoryLLM(), now=t(13, 8))             # my reply is the latest mail
    assert rep.digest[0].count == 2 and rep.digest[0].insight.needs_reply is False
    assert "overdue_reply" not in [f.kind for f in rep.missed]


def test_conversations_with_ids_are_not_joined_by_subject():
    a = mail("y1", "견적 요청", "sales@vendor-a.com", t(14, 9), "견적", to=("team@corp.com",), conv="cv-a")
    b = mail("y2", "견적 요청", "sales@vendor-b.com", t(15, 9), "견적", to=("team@corp.com",), conv="cv-b")
    assert len(group_threads([a, b], "")) == 2


def test_revision_is_stable_when_old_mails_leave_the_window(scfg):
    src, llm = ListSource(base_mails()), StoryLLM()
    _run(scfg, src, llm)
    calls = llm.calls
    _run(scfg, src, llm, now=t(19, 10))       # s1 (11/12 09:00) is now outside the 7-day window; no new mail
    assert not any("Q4 MPE" in u.split("OPEN STORIES")[0] for u in llm.users[calls - 1:])
    tr = _tracker(scfg)
    assert len(tr.updates(tr.stories()[0].id)) == 1


# ---------------------------------------------------------------- judge: not sure -> review, never story

@pytest.mark.parametrize("cat,story,route,reasons", [
    ("action_required", StoryVerdict(True, "me", "고객이 승인하면 종료", confidence=0.9), "story", []),
    ("action_required", StoryVerdict(True, "", "고객이 승인하면 종료", confidence=0.9), "review", ["owner_unknown"]),
    ("action_required", StoryVerdict(True, "me", "", confidence=0.9), "review", ["end_unknown"]),
    ("action_required", StoryVerdict(True, "me", "고객이 승인하면 종료", confidence=0.6), "review", ["low_confidence"]),
    ("announcement", StoryVerdict(True, "팀", "회신하면 종료", confidence=0.9), "review", ["category_conflict"]),
    ("newsletter", StoryVerdict(False), "none", []),
    ("action_required", StoryVerdict(False, "me", "회신하면 종료", confidence=0.9), "none", []),  # one-off task
])
def test_judge_table(cat, story, route, reasons):
    th = group_threads([mail("j1", "제목", "x@corp.com", NOW, "본문")], ME)[0]
    v = judge(th, MailInsight("j1", "s", cat, 2, False, story=story), StoryConfig(), ME)
    assert (v.route, v.reasons) == (route, reasons)


@pytest.mark.parametrize("raw,val", [("high", 0.9), (0.85, 0.85), (90, 0.9), (1, 0.0), (True, 0.0), ("3", 0.0),
                                     (None, 0.0)])
def test_confidence_scale(raw, val):
    from mailbrief.extract import Normalizer
    from mailbrief.config import DEFAULT_CATEGORIES
    assert Normalizer(dict(DEFAULT_CATEGORIES)).confidence(raw) == val


def test_automatic_sender_is_noise():
    th = group_threads([mail("j2", "알림", "noreply@corp.com", NOW, "본문")], ME)[0]
    v = judge(th, MailInsight("j2", "s", "fyi", 3, False, story=StoryVerdict(True, "me", "확인하면 종료", confidence=0.9)),
              StoryConfig(), ME)
    assert v.noise and v.route == "review" and "category_conflict" in v.reasons


def test_llm_failure_is_never_a_story(scfg):
    rep = _run(scfg, ListSource(base_mails()), StoryLLM(fail_on="Q4 MPE"))
    assert rep.stats["routes"]["pending"] == 1 and rep.stats["routes"]["story"] == 0
    assert _tracker(scfg).stories() == []


# ---------------------------------------------------------------- the 79/131 problem: no match != story

def test_unmatched_mails_are_not_promoted(scfg):
    rep = _run(scfg, ListSource(base_mails()), StoryLLM(), deliver=True)
    r = rep.stats["routes"]
    assert (r["story"], r["review"], r["none"]) == (1, 2, 6)
    tr = _tracker(scfg)
    assert [s.title for s in tr.stories()] == ["Q4 MPE 검증"]
    reviews = {i.subject: i.reasons for i in tr.open_reviews()}
    # real work in a notice goes to review (not absorbed silently); the ambiguous thread goes to review
    assert reviews["[공지] 사내 시스템 이관 및 팀별 담당자 지정 요청"] == ["category_conflict"]
    assert reviews["협력사 샘플 문의"] == ["owner_unknown", "end_unknown", "low_confidence"]
    page = Path(scfg.output.dir, "stories.html").read_text(encoding="utf-8")
    assert "S1" in page and "R1" in page and "Newsletter" not in page and "보안교육" not in page
    assert [c.title for c in __import__("mailbrief.cards", fromlist=["x"]).build_cards(rep)].count("스토리") == 1


# ---------------------------------------------------------------- updates: same-day drafts are kept

def test_same_day_second_update_is_kept_and_afternoon_is_latest(scfg):
    src = ListSource(base_mails())
    llm = StoryLLM()
    _run(scfg, src, llm)                                    # morning capture
    src.mails.append(mail("s4", "RE: Q4 MPE 검증 진행 요청", "kim.manager@corp.com", t(17, 15, 40),
                          "고객 리뷰 일정 확정: 11/20 14시.", conv="conv-mpe"))
    _run(scfg, src, llm, now=t(17, 16))                     # afternoon, same day
    tr = _tracker(scfg)
    st = tr.stories()[0]
    ups = tr.updates(st.id)
    assert len(ups) == 2
    assert ups[0]["email_id"] == "s4" and "확정" in ups[0]["summary"]   # newer by full time, not by date
    calls = llm.calls
    _run(scfg, src, llm, now=t(17, 17))                     # no new mail: no LLM call for threads, no new update
    assert llm.calls == calls + 1 and len(tr.updates(st.id)) == 2


def test_merge_keeps_all_updates_and_clears_duplicate_flag(scfg):
    tr = _tracker(scfg)
    a = tr._create("장비 이관", "", "me", "이관 완료 확인", "2026-11-10T00:00:00+00:00")
    b = tr._create("장비 이관 준비", "", "", "", "2026-11-11T00:00:00+00:00")
    assert b.flags == [f"possible_duplicate:{a.sid}"]
    tr._add_update(a.id, "c:1", "r1", "e1", "s", "오전 업데이트", "2026-11-17T00:10:00+00:00", NOW)
    tr._add_update(b.id, "c:2", "r2", "e2", "s", "오후 업데이트", "2026-11-17T06:40:00+00:00", NOW)
    tr._link(b.id, ["c:2"], "new", "2026-11-17T06:40:00+00:00")
    dst = tr.merge(b.sid, a.sid)
    assert [u["summary"] for u in tr.updates(dst.id)] == ["오후 업데이트", "오전 업데이트"]
    assert tr.get(b.id, follow=False).status == "merged" and tr.get(b.sid).id == a.id
    assert [l["key"] for l in tr.links(a.id)] == ["c:2"]
    with pytest.raises(ValueError):
        tr.merge(a.sid, a.sid)


# ---------------------------------------------------------------- review state is derived

def test_review_clears_when_owner_and_end_are_set(scfg):
    _run(scfg, ListSource(base_mails()), StoryLLM())
    tr = _tracker(scfg)
    item = next(i for i in tr.open_reviews() if i.subject == "협력사 샘플 문의")
    st = tr.promote(item.rid, NOW)                          # promoted without owner / end condition
    assert st.review_reasons() == ["owner_unknown", "end_unknown"]
    assert st.sid in [s.sid for s in tr.needs_review()]
    st = tr.set_fields(st.sid, owner="김책임", end_condition="샘플 평가 보고서가 승인되면 종료")
    assert st.review_reasons() == [] and tr.needs_review() == []
    assert tr.review(item.rid).status == "promoted"


def test_open_review_is_promoted_when_a_later_mail_makes_it_clear(scfg):
    src = ListSource(base_mails())
    llm = StoryLLM()
    _run(scfg, src, llm)
    tr = _tracker(scfg)
    item = next(i for i in tr.open_reviews() if i.subject == "협력사 샘플 문의")
    src.mails.append(mail("v2", "RE: 협력사 샘플 문의", "partner@vendor.com", t(17, 9),
                          "샘플 평가 담당은 신승빈 책임입니다. 평가 보고서 승인 시 종료합니다.", conv="conv-vendor"))
    rep = _run(scfg, src, llm, now=t(17, 10))
    assert tr.review(item.rid).status == "promoted"
    assert "협력사 샘플 평가" in [s.title for s in tr.stories()]
    assert rep.stories["created"][0][1] == "협력사 샘플 평가"


def test_dismissed_thread_stays_out_until_a_clear_story(scfg):
    src = ListSource(base_mails())
    llm = StoryLLM()
    _run(scfg, src, llm)
    tr = _tracker(scfg)
    item = next(i for i in tr.open_reviews() if i.subject == "협력사 샘플 문의")
    tr.dismiss(item.rid, NOW)
    src.mails.append(mail("v3", "RE: 협력사 샘플 문의", "partner@vendor.com", t(17, 9), "추가 문의입니다.", conv="conv-vendor"))
    _run(scfg, src, llm, now=t(17, 10))
    assert tr.review(item.rid).status == "dismissed"       # still ambiguous: the user decision stays
    src.mails.append(mail("v4", "RE: 협력사 샘플 문의", "partner@vendor.com", t(17, 11),
                          "담당은 신승빈 책임, 보고서 승인 시 종료.", conv="conv-vendor"))
    _run(scfg, src, llm, now=t(17, 12))
    again = tr.review(item.rid)
    assert again.status == "open" and again.reasons == ["reopened"]   # a clear story: ask again, do not auto-create
    _run(scfg, src, llm, now=t(17, 13))                     # next run, no user action: still a question
    assert tr.review(item.rid).status == "open" and "협력사 샘플 평가" not in [s.title for s in tr.stories()]


def test_dismissed_thread_is_not_attached_by_llm_match(scfg):
    from story_fixture import CANNED, ans, st
    src, llm = ListSource(base_mails()), StoryLLM()
    added = [("예약 확정", ans("장비 예약 확정.", "fyi", st(False, None, None, "low", match="S1"))),
             ("추가 작업 문의", ans("장비 추가 작업 문의.", "fyi", st(True, None, None, "medium")))]
    CANNED[0:0] = added
    try:
        _run(scfg, src, llm)                                # S1 exists now
        src.mails.append(mail("e0", "장비 사용 문의", "park@corp.com", t(17, 9), "추가 작업 문의입니다.", conv="conv-eq"))
        _run(scfg, src, llm, now=t(17, 10))
        tr = _tracker(scfg)
        item = next(i for i in tr.open_reviews() if i.subject == "장비 사용 문의")
        tr.dismiss(item.rid, NOW)
        src.mails.append(mail("e1", "RE: 장비 사용 문의", "park@corp.com", t(17, 11), "예약 확정입니다.", conv="conv-eq"))
        _run(scfg, src, llm, now=t(17, 12))                 # LLM says "match S1", but the user dismissed this thread
        assert "c:conv-eq" not in [l["key"] for l in tr.links(tr.stories()[0].id)]
        assert tr.review(item.rid).status == "dismissed"
    finally:
        del CANNED[0:2]


def test_llm_match_attaches_to_open_story_and_bad_match_is_ignored(scfg):
    src = ListSource(base_mails())
    llm = StoryLLM()
    _run(scfg, src, llm)
    src.mails.append(mail("e1", "MPE 측정 장비 예약", "park@corp.com", t(17, 9), "장비 예약 완료.", conv="conv-equip"))
    _run(scfg, src, llm, now=t(17, 10))
    tr = _tracker(scfg)
    st = tr.stories()[0]
    assert "OPEN STORIES" in llm.users[-1] and "S1: Q4 MPE 검증" in llm.users[-1]
    assert [u["email_id"] for u in tr.updates(st.id)][0] == "e1"
    assert "c:conv-equip" in [l["key"] for l in tr.links(st.id)]
    assert tr.unlink(st.sid, "c:conv-equip") == 1
    _run(scfg, src, llm, now=t(17, 11))                     # cached match must not attach it again
    assert "c:conv-equip" not in [l["key"] for l in tr.links(st.id)]


def test_same_generic_subject_later_is_another_thread(scfg):
    tr = _tracker(scfg)
    st_ = tr.new("자료 정리", NOW, owner="me", end="자료 제출 시 종료")
    k1 = mail("k1", "자료 요청", "kim@corp.com", t(10, 9), "자료 부탁")
    kim = group_threads([k1], ME)[0]
    tr._link(st_.id, sorted(kim.keys), "new", "2026-11-10T00:00:00+00:00", kim)
    hr = group_threads([mail("h9", "자료 요청", "hr@corp.com", t(17, 9), "교육 자료")], ME)[0]
    assert hr.key != kim.key and tr.linked_story(hr) is None       # same subject, other thread: no attach
    k2 = mail("k2", "RE: 자료 요청", "kim@corp.com", t(12, 9), "추가 자료")
    both = group_threads([k1, k2], ME)[0]
    assert len(both.mails) == 2 and tr.linked_story(both).id == st_.id   # shares mail k1: same thread


def test_promote_twice_is_refused(scfg):
    _run(scfg, ListSource(base_mails()), StoryLLM())
    tr = _tracker(scfg)
    rid = tr.open_reviews()[0].rid
    tr.promote(rid, NOW, owner="me", end="끝나면 종료")
    with pytest.raises(ValueError):
        tr.promote(rid, NOW)


def test_end_reached_needs_confirmation_not_auto_close(scfg):
    src = ListSource(base_mails())
    llm = StoryLLM()
    _run(scfg, src, llm)
    src.mails.append(mail("s5", "RE: Q4 MPE 검증 진행 요청", "kim.manager@corp.com", t(17, 11), "고객 승인 완료.",
                          conv="conv-mpe"))
    _run(scfg, src, llm, now=t(17, 12))
    tr = _tracker(scfg)
    st = tr.stories()[0]
    assert st.status == "active" and "end_reached" in st.review_reasons()
    assert tr.close(st.sid).status == "done"


# ---------------------------------------------------------------- folder names and export

@pytest.mark.parametrize("raw,safe", [
    ("Q4/Q1 계획", "Q4-Q1 계획"), ("a\\b:c*d?e\"f<g>h|i", "a-b-c-d-e-f-g-h-i"), ("../../etc", "etc"),
    ("CON", "_CON"), ("이름. ", "이름"), ("", "untitled"), ("x" * 100, "x" * 60),
])
def test_safe_name(raw, safe):
    assert safe_name(raw) == safe


def test_export_moves_folder_and_keeps_user_files(scfg, tmp_path):
    _run(scfg, ListSource(base_mails()), StoryLLM())
    tr = _tracker(scfg)
    st = tr.stories()[0]
    tr.set_fields(st.sid, title="Q4/MPE 검증")
    root = tmp_path / "stories"
    export_folders(tr, root, TZ)
    d = root / "active" / "S1 Q4-MPE 검증"
    assert (d / "STORY.md").is_file()
    (d / "memo.txt").write_text("내 메모", encoding="utf-8")
    reviews = sorted(p.name for p in (root / "review").glob("*.md"))
    assert len(reviews) == 2
    tr.close(st.sid)
    tr.dismiss(tr.open_reviews()[0].rid, NOW)
    export_folders(tr, root, TZ)
    assert not d.exists() and (root / "done" / "S1 Q4-MPE 검증" / "memo.txt").read_text(encoding="utf-8") == "내 메모"
    assert len(list((root / "review").glob("*.md"))) == 1
    (root / "review" / "my note.md").write_text("손으로 쓴 파일", encoding="utf-8")
    (root / "review" / "old memo.md").write_bytes("메모장 cp949 파일".encode("cp949"))
    export_folders(tr, root, TZ)
    assert (root / "review" / "my note.md").exists()       # not generated -> never removed
    assert (root / "review" / "old memo.md").exists()      # not UTF-8: no crash, not removed


# ---------------------------------------------------------------- accuracy measurement and source check

def test_eval_export_and_score(scfg, tmp_path):
    src = ListSource(base_mails())
    store = Store(scfg.output.db_path)
    _, threads = fetch_threads(scfg, src, NOW - timedelta(days=7))
    tr = make_tracker(scfg, store)
    insights, _ = analyze_threads(scfg, threads, StoryLLM(), store)
    rows = predict_rows(scfg, threads, insights, tr, ME)
    assert tr.stories() == [] and tr.open_reviews() == []  # export does not change the tracker
    gold = {"Q4 MPE": "story", "협력사 샘플": "스토리", "시스템 이관": "story", "Newsletter": "아님"}
    for r in rows:
        r["gold_route"] = next((g for k, g in gold.items() if k in r["subject"]), "none")
    p = write_csv(rows, tmp_path / "eval.csv")
    s = score(read_csv(p))
    assert s["labeled"] == 9 and s["story_precision"] == 1.0 and s["story_caught"] == 1.0
    assert s["confusion"]["story"] == {"story": 1, "review": 2}
    assert "스토리 정밀도" in format_score(s)


def test_eval_uses_the_tracker_decision(scfg):
    src, llm = ListSource(base_mails()), StoryLLM()
    _run(scfg, src, llm)
    src.mails.append(mail("e1", "MPE 측정 장비 예약", "park@corp.com", t(17, 9), "장비 예약 완료.", conv="conv-equip"))
    store = Store(scfg.output.db_path)
    tr = make_tracker(scfg, store)
    _, threads = fetch_threads(scfg, src, t(17, 10) - timedelta(days=7))
    insights, _ = analyze_threads(scfg, threads, llm, store, open_stories=tr.prompt_stories())
    rows = {r["subject"]: r for r in predict_rows(scfg, threads, insights, tr, ME)}
    assert rows["MPE 측정 장비 예약"]["pred_route"] == "story" and rows["MPE 측정 장비 예약"]["linked_story"] == "S1"
    assert rows["Q4 MPE 검증 진행 요청"]["linked_story"] == "S1"


def test_check_source_reports_threads_and_copies(scfg):
    out = check_source(scfg, 7, source=ListSource(base_mails()), now=NOW)
    assert "사본 제거: 1통" in out and "스레드: 9개" in out and "자동 발신 메일: 2통" in out


# ---------------------------------------------------------------- CLI and watch

def test_story_cli_flow(scfg, capsys):
    _run(scfg, ListSource(base_mails()), StoryLLM())
    store = Store(scfg.output.db_path)
    tr = StoryTracker(store, scfg.stories, ME)
    rid = next(i.rid for i in tr.open_reviews() if "시스템 이관" in i.subject)
    assert story_command(scfg, "promote", [rid], owner="me", end="이관 데이터 검증 완료 회신", store=store, now=NOW) == 0
    assert story_command(scfg, "list", [], store=store, now=NOW) == 0
    assert story_command(scfg, "show", ["S2"], store=store, now=NOW) == 0
    assert story_command(scfg, "promote", ["R99"], store=store, now=NOW) == 2
    out = capsys.readouterr().out
    assert f"{rid} -> S2" in out and "진행 중 2건" in out and "no review item R99" in out
    assert Path(scfg.output.dir, "stories.html").is_file()
    assert story_command(scfg, "new", [], title="장비 이관", owner="me", end="이관 완료 확인", store=store, now=NOW) == 0
    assert story_command(scfg, "new", [], store=store, now=NOW) == 2   # title is required
    assert "S3" in [s.sid for s in tr.stories()]


def test_watch_updates_story_with_new_reply(scfg):
    from mailbrief.watch import watch
    src = ListSource(base_mails())
    llm = StoryLLM()
    store = Store(scfg.output.db_path)
    _run(scfg, src, llm)
    watch(scfg, once=True, popup=False, llm=llm, source=src, store=store, clock=lambda: NOW)   # marks current mails
    src.mails.append(mail("s6", "RE: Q4 MPE 검증 진행 요청", "kim.manager@corp.com", t(17, 9, 5),
                          "고객 리뷰 일정 확정: 11/20.", conv="conv-mpe"))
    watch(scfg, once=True, popup=False, llm=llm, source=src, store=store, clock=lambda: t(17, 9, 10))
    tr = StoryTracker(store, scfg.stories, ME)
    assert tr.updates(tr.stories()[0].id)[0]["email_id"] == "s6"
