"""`mailbrief story ...`: list the stories and the review queue, decide review items, edit stories.

  mailbrief story                                   review queue + active stories
  mailbrief story show S3 | R2                      details and update history
  mailbrief story new --title T --owner X --end ".." register a story by hand (migration from an old tracker)
  mailbrief story promote R2 --owner X --end "..."  review item -> new story
  mailbrief story promote R2 --into S3              review item -> existing story
  mailbrief story dismiss R2                        "not a story" (later mails of the thread stay out)
  mailbrief story set S3 --owner X --end "..."      edit; the review reasons go away when owner and end are set
  mailbrief story confirm S3                        clear flags (possible duplicate, end reached, new activity)
  mailbrief story close S3 | reopen S3
  mailbrief story merge S4 S3                       S4 into S3. Updates are kept.
  mailbrief story unlink S3 --key c:...             remove a thread from a story

After a change, the dashboard (out/stories.html) and the story folders are written again.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config, expand
from .dashboard import _local, owner_label
from .stories import StoryTracker, reason_ko
from .store import Store

ACTIONS = ["list", "show", "new", "promote", "dismiss", "set", "confirm", "close", "reopen", "merge", "unlink"]


def _print_story(tr: StoryTracker, st, tz, full: bool = False) -> None:
    reasons = st.review_reasons()
    print(f"[{st.sid}] {st.title}  ({st.status})")
    print(f"    목표: {st.goal or '-'}")
    print(f"    담당: {owner_label(st.owner) or '(미확인)'} · 종결: {st.end_condition or '(미확인)'} · 활동 {_local(st.updated, tz)}")
    if reasons:
        print(f"    검토: {', '.join(reason_ko(r) for r in reasons)}")
    if full:
        print("    업데이트:")
        for u in tr.updates(st.id):
            print(f"      - {_local(u['source_time'], tz)} {u['subject']} — {u['summary']}")
        print("    스레드: " + ", ".join(l["key"] for l in tr.links(st.id)))


def _print_review(item, tz) -> None:
    p = item.proposal
    print(f"[{item.rid}] {item.subject}  ({item.status}, {_local(item.source_time, tz)})")
    print(f"    요약: {item.summary}")
    print(f"    사유: {', '.join(reason_ko(r) for r in item.reasons)}")
    print(f"    제안: {p.get('title', '')} · 담당 {p.get('owner') or '?'} · 종결 {p.get('end_condition') or '?'}")


def story_command(cfg: Config, action: str, ids: list[str], owner=None, end=None, title=None, goal=None,
                  into=None, key=None, store: Store | None = None, now: datetime | None = None) -> int:
    from .pipeline import write_story_views

    tz = ZoneInfo(cfg.analysis.timezone)
    now = now or datetime.now(tz)
    store = store or Store(expand(cfg.output.db_path))
    tr = StoryTracker(store, cfg.stories, cfg.source.my_address or cfg.source.email)
    need = {"show": 1, "promote": 1, "dismiss": 1, "set": 1, "confirm": 1, "close": 1, "reopen": 1, "merge": 2,
            "unlink": 1}
    if len(ids) < need.get(action, 0):
        print(f"usage: mailbrief story {action} " + ("SRC DST" if action == "merge" else "ID"))
        return 2
    try:
        if action == "list":
            reviews, flagged = tr.open_reviews(), tr.needs_review()
            print(f"== 검토 대기 {len(reviews) + len(flagged)}건 ==")
            for i in reviews:
                _print_review(i, tz)
            for s in flagged:
                _print_story(tr, s, tz)
            active = [s for s in tr.stories(("active",)) if not s.review_reasons()]
            print(f"\n== 진행 중 {len(active)}건 ==")
            for s in active:
                _print_story(tr, s, tz)
            return 0
        if action == "show":
            x = ids[0].strip().upper()
            if x.startswith("R"):
                item = tr.review(x)
                if not item:
                    raise ValueError(f"no review item {x}")
                _print_review(item, tz)
            else:
                _print_story(tr, tr._need(x), tz, full=True)
            return 0
        if action == "new":
            st = tr.new(title or "", now, goal=goal or "", owner=owner or "", end=end or "")
            _print_story(tr, st, tz)
        elif action == "promote":
            st = tr.promote(ids[0], now, title=title or "", owner=owner or "", end=end or "", goal=goal or "",
                            into=into or "")
            print(f"{ids[0].upper()} -> {st.sid}")
            _print_story(tr, st, tz)
        elif action == "dismiss":
            tr.dismiss(ids[0], now)
            print(f"{ids[0].upper()}: 스토리 아님으로 처리")
        elif action == "set":
            _print_story(tr, tr.set_fields(ids[0], owner=owner, end_condition=end, title=title, goal=goal), tz)
        elif action == "confirm":
            _print_story(tr, tr.confirm(ids[0]), tz)
        elif action == "close":
            _print_story(tr, tr.close(ids[0]), tz)
        elif action == "reopen":
            _print_story(tr, tr.reopen(ids[0]), tz)
        elif action == "merge":
            st = tr.merge(ids[0], ids[1])
            print(f"{ids[0].upper()} -> {st.sid}")
            _print_story(tr, st, tz)
        elif action == "unlink":
            if not key:
                print("usage: mailbrief story unlink S3 --key <thread key from `story show`>")
                return 2
            print(f"removed links: {tr.unlink(ids[0], key)}")
    except ValueError as e:
        print(f"error: {e}")
        return 2
    try:
        write_story_views(cfg, tr, now)
    except Exception as e:  # the change is saved; only the views failed
        print(f"warning: dashboard / folders not written: {e}")
    return 0
