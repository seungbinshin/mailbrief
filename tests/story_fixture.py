"""Mails and a fake LLM for the story tests. The threads repeat the problems measured in the in-house story tracker:
newsletters, automatic alerts and training notices became stories; real work in a notice was absorbed silently;
RE: mails were counted as different mails."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from mailbrief.llm import LLMError
from mailbrief.models import Email

TZ = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 17, 8, 30, tzinfo=TZ)
ME = "me@corp.com"


def t(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm, tzinfo=TZ)


def mail(id, subject, sender, received, body, to=(ME,), cc=(), conv="", folder="inbox", name="", mid="", **kw):
    return Email(id=id, subject=subject, sender=sender, sender_name=name or sender.split("@")[0], received=received,
                 body_text=body, to=list(to), cc=list(cc), conversation_id=conv, folder=folder, message_id=mid, **kw)


def base_mails() -> list[Email]:
    return [
        # T1: a real story (3 mails, one of them my reply in Sent Items)
        mail("s1", "Q4 MPE 검증 진행 요청", "kim.manager@corp.com", t(12, 9),
             "신승빈 책임이 Q4 MPE 검증을 맡아 주세요. 고객 승인까지 매주 결과를 공유 바랍니다.", conv="conv-mpe", name="김팀장"),
        mail("s2", "RE: Q4 MPE 검증 진행 요청", ME, t(13, 14), "1차 결과 공유합니다. 2차는 다음 주에 공유하겠습니다.",
             to=("kim.manager@corp.com",), conv="conv-mpe", folder="sent", name="신승빈"),
        mail("s3", "RE: RE: Q4 MPE 검증 진행 요청", "kim.manager@corp.com", t(16, 10),
             "2차 결과는 11/20 고객 리뷰에서 봅니다.", conv="conv-mpe", name="김팀장"),
        # T2: newsletter
        mail("n1", "[Newsletter] 이번 주 기술 동향", "news@corp.com", t(16, 9), "기술 동향입니다. 수신거부는 여기.",
             to=("all@corp.com",), conv="conv-news"),
        # T3: automatic alert
        mail("a1", "[Jenkins] 빌드 실패 알림", "jenkins-notification@corp.com", t(16, 22), "빌드 #512 실패.",
             conv="conv-ci"),
        # T4: training notice with a one-off action
        mail("h1", "필수 보안교육 안내", "hr@corp.com", t(13, 11), "11/20(금) 18:00까지 온라인 보안교육을 이수하세요.",
             conv="conv-hr"),
        # T5: a notice that hides real work (must not be absorbed silently)
        mail("i1", "[공지] 사내 시스템 이관 및 팀별 담당자 지정 요청", "it-admin@corp.com", t(15, 9),
             "12월 시스템 이관을 진행합니다. 팀별 이관 담당자가 데이터 검증을 끝내고 완료 회신을 주셔야 합니다.",
             to=("team@corp.com",), conv="conv-it"),
        # T6: ambiguous: work continues, but no owner and no end condition
        mail("v1", "협력사 샘플 문의", "partner@vendor.com", t(16, 15), "샘플 관련해서 다음 주에 다시 연락드리겠습니다.",
             conv="conv-vendor"),
        # T7: one-off reply request
        mail("r1", "설계 리뷰 자료 회신 부탁", "lee@corp.com", t(14, 9), "11/18까지 의견 회신 부탁드립니다.", conv="conv-review"),
        # T9: no conversation id: RE:/회신: must still be one thread (shared participant team@)
        mail("w1", "주간 보고 취합", "choi@corp.com", t(14, 9), "금요일까지 주간 보고 부탁드립니다.", to=("team@corp.com",)),
        mail("w2", "RE: 주간 보고 취합", "park@corp.com", t(14, 13), "제출했습니다.", to=("team@corp.com",)),
        mail("w3", "회신: 주간 보고 취합", "lee@corp.com", t(15, 10), "저도 제출했습니다.", to=("team@corp.com",)),
        # T10: one mail, two copies (inbox + sent): same Message-ID
        mail("c1", "팀 회의록 공유", ME, t(16, 18), "오늘 회의록입니다.", to=("team@corp.com",), conv="conv-minutes",
             mid="<minutes-1@corp.com>"),
        mail("c1-sent", "팀 회의록 공유", ME, t(16, 18), "오늘 회의록입니다.", to=("team@corp.com",), conv="conv-minutes",
             mid="<minutes-1@corp.com>", folder="sent"),
    ]


def ans(summary, category, story=None, priority=2, needs_reply=False, actions=(), meeting_type=None):
    return {"summary": summary, "category": category, "priority": priority, "needs_reply": needs_reply,
            "action_items": list(actions), "events": [], "meeting_type": meeting_type,
            "story": story or {"persistence": False, "owner": None, "end_condition": None, "title": "",
                               "goal": "", "confidence": "low", "done": False, "match": None}}


def st(persistence, owner, end, conf, title="", goal="", done=False, match=None):
    return {"persistence": persistence, "owner": owner, "end_condition": end, "title": title, "goal": goal,
            "confidence": conf, "done": done, "match": match}


MPE_STORY = st(True, "me", "고객이 Q4 MPE 검증 리포트를 승인하면 종료", "high", "Q4 MPE 검증", "Q4 MPE 검증 결과로 고객 승인을 받는다")

# Checked in this order; the first key found in the thread text wins (more specific keys first).
CANNED = [
    ("고객 리뷰 일정 확정", ans("고객 리뷰가 11/20 14시로 확정됨.", "action_required", MPE_STORY)),
    ("고객 승인 완료", ans("고객이 최종 리포트를 승인함.", "fyi", {**MPE_STORY, "done": True})),
    ("Q4 MPE", ans("김팀장이 Q4 MPE 검증을 요청. 1차 공유 완료, 2차는 11/20 고객 리뷰.", "action_required", MPE_STORY)),
    ("Newsletter", ans("주간 기술 동향 뉴스레터.", "newsletter", priority=3)),
    ("Jenkins", ans("빌드 #512 실패 알림.", "fyi", priority=3)),
    ("보안교육", ans("11/20까지 필수 보안교육 이수.", "announcement",
                  actions=[{"description": "보안교육 이수", "due": "2026-11-20T18:00:00+09:00", "owner": "me", "msg": 1}])),
    ("시스템 이관", ans("12월 시스템 이관. 팀별 담당자가 데이터 검증 후 완료 회신.", "announcement",
                    st(True, "팀별 이관 담당자", "데이터 검증 후 완료 회신을 보내면 종료", "high", "시스템 이관 데이터 검증"))),
    ("담당은 신승빈", ans("협력사 샘플 평가. 담당 신승빈, 평가 보고서 승인 시 종료.", "action_required",
                     st(True, "me", "샘플 평가 보고서가 승인되면 종료", "high", "협력사 샘플 평가"))),
    ("협력사 샘플", ans("협력사가 샘플 건으로 다음 주 다시 연락 예정.", "fyi", st(True, None, None, "medium", "협력사 샘플"))),
    ("설계 리뷰", ans("이책임이 설계 리뷰 의견을 11/18까지 요청.", "action_required", needs_reply=True,
                  actions=[{"description": "설계 리뷰 의견 회신", "due": "2026-11-18T18:00:00+09:00", "owner": "me", "msg": 1}])),
    ("주간 보고", ans("주간 보고 취합. 대부분 제출함.", "action_required")),
    ("회의록", ans("팀 회의록 공유.", "fyi", priority=3)),
    ("장비 예약", ans("MPE 측정 장비를 11/19로 예약함.", "fyi", priority=3, story=st(False, None, None, "low", match="S1"))),
]


class StoryLLM:
    def __init__(self, fail_on: str | None = None):
        self.calls = 0
        self.users: list[str] = []
        self.fail_on = fail_on

    def chat(self, system, user, json_output=True):
        self.calls += 1
        if not json_output:
            return "- 오늘 할 일 정리"
        self.users.append(user)
        thread = user.split("\nOPEN STORIES")[0]
        if self.fail_on and self.fail_on in thread:
            raise LLMError("simulated failure")
        for k, v in CANNED:
            if k in thread:
                return json.dumps(v, ensure_ascii=False)
        return json.dumps(ans("기타", "other"), ensure_ascii=False)


class ListSource:
    """In-memory mail source: tests add mails between runs."""

    def __init__(self, mails: list[Email]):
        self.mails = list(mails)

    def fetch_mails(self, since, limit):
        return sorted([m for m in self.mails if m.folder == "inbox" and m.received >= since],
                      key=lambda m: m.received, reverse=True)[:limit]

    def fetch_sent(self, since, limit):
        return [m for m in self.mails if m.folder == "sent" and m.received >= since][:limit]

    def fetch_events(self, start, end):
        return []

    def send_to_self(self, subject, html_body):
        pass
