import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mailbrief.config import Config  # noqa: E402
from mailbrief.llm import LLMError  # noqa: E402

FIX = Path(__file__).parent / "fixtures"

# Canned answers that a well-behaved model returns for each fixture mail.
CANNED = {
    "Q4 MPE": {"summary": "김팀장이 Q4 MPE 검증 리포트를 수요일 18시까지 요청.", "category": "action_required",
               "priority": 1, "needs_reply": True,
               "action_items": [{"description": "Q4 MPE 검증 리포트 공유", "due": "2026-11-18T18:00:00+09:00", "owner": "me"}],
               "events": []},
    "설계 리뷰": {"summary": "이책임이 설계 리뷰 자료 의견을 11/14까지 요청.", "category": "action_required",
              "priority": 2, "needs_reply": True,
              "action_items": [{"description": "설계 리뷰 자료 의견 회신", "due": "2026-11-14T18:00:00+09:00", "owner": "me"}],
              "events": []},
    "고객사 방문": {"summary": "11/19 14시 고객사 방문 안내.", "category": "meeting", "priority": 2, "needs_reply": False,
               "action_items": [],
               "events": [{"title": "고객사 방문", "start": "2026-11-19T14:00:00+09:00", "end": None, "location": "이천 본사 3층"}]},
    "Newsletter": {"summary": "주간 기술 동향 뉴스레터.", "category": "newsletter", "priority": 3, "needs_reply": False,
                   "action_items": [], "events": []},
    "보안교육": {"summary": "11/15 10시 필수 보안교육.", "category": "announcement", "priority": 2, "needs_reply": False,
             "action_items": [{"description": "필수 보안교육 이수", "due": "2026-11-15T10:00:00+09:00", "owner": "me"}],
             "events": [{"title": "필수 보안교육", "start": "2026-11-15T10:00:00+09:00", "end": "2026-11-15T11:00:00+09:00", "location": "온라인"}]},
    "Weekly sync": {"summary": "Weekly sync 회의 초대.", "category": "meeting", "priority": 2, "needs_reply": False,
                    "action_items": [],
                    "events": [{"title": "Weekly sync", "start": "2026-11-18T10:00:00+09:00", "end": "2026-11-18T11:00:00+09:00", "location": ""}]},
    "실험 데이터": {"summary": "실험 데이터 정리 메모.", "category": "other", "priority": 3, "needs_reply": False,
               "action_items": [], "events": []},
}


class FakeLLM:
    def __init__(self, fail_on: str | None = None, fenced: bool = False):
        self.calls = 0
        self.fail_on = fail_on
        self.fenced = fenced

    def chat(self, system, user, json_output=True):
        self.calls += 1
        if not json_output:
            return "- 오늘 Daily standup 10:00 참석\n- 설계 리뷰 의견 회신이 지연됨\n- Q4 MPE 리포트 마감 11/18"
        if self.fail_on and self.fail_on in user:
            raise LLMError("simulated failure")
        for k, v in CANNED.items():
            if k in user:
                body = json.dumps(v, ensure_ascii=False)
                return f"결과입니다:\n```json\n{body}\n```" if self.fenced else body
        return json.dumps({"summary": "unknown", "category": "other", "priority": 3, "needs_reply": False,
                           "action_items": [], "events": []})


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.source.type = "fixture"
    c.source.fixture_dir = str(FIX)
    c.source.my_address = "me@corp.com"
    c.analysis.skip_senders = ["noreply@corp.com"]
    c.output.dir = str(tmp_path / "out")
    c.output.db_path = str(tmp_path / "state.sqlite3")
    c.output.popup = False
    return c
