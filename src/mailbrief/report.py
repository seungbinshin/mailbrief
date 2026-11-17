"""Render the report as Markdown and HTML (HTML is mail-client safe: inline styles only)."""
from __future__ import annotations

import html

from .analyze import Report

_CAT_KO = {"action_required": "조치 필요", "meeting": "회의", "fyi": "참고", "announcement": "공지",
           "newsletter": "뉴스레터", "other": "기타"}
_PRIO = {1: "🔴", 2: "🟡", 3: "⚪"}
_KIND_KO = {"unanswered_invite": "미응답 회의", "overdue_action": "기한 지난 할 일", "missed_mail_event": "캘린더 누락 일정",
            "overdue_reply": "회신 지연", "upcoming_deadline": "마감", "mail_event_not_on_calendar": "캘린더 미등록 일정"}
_WD = "월화수목금토일"
_MEETING_KO = {"regular": "정기", "review": "리뷰", "decision": "결정", "external": "외부", "other": "기타"}


def cat_label(i) -> str:
    """'조치 필요' or '회의·리뷰' (meeting type from the body and invite, not from the subject only)."""
    c = _CAT_KO.get(i.category, i.category)
    return f"{c}·{_MEETING_KO.get(i.meeting_type, i.meeting_type)}" if i.meeting_type else c


def thread_mark(d) -> str:
    return f" ({d.count}통)" if d.count > 1 else ""


def story_lines(st: dict) -> list[str]:
    """Plain lines for the story part of the report (empty when nothing happened)."""
    if not st:
        return []
    L = [f"- 새 스토리 [{sid}] {title}" for sid, title, _ in st.get("created", [])]
    L += [f"- 업데이트 [{sid}] {title} ← {subj}" for sid, title, subj in st.get("updated", [])]
    L += [f"- 검토 필요 [{rid}] {subj} ({why})" for rid, subj, why in st.get("review_new", [])]
    r = st.get("routes", {})
    if r.get("threads"):
        L.append(f"- 판정: 스레드 {r['threads']}개 중 스토리 {r.get('story', 0)} · 검토 {r.get('review', 0)} · "
                 f"아님 {r.get('none', 0)}" + (f" · 보류(LLM 실패) {r['pending']}" if r.get("pending") else ""))
    L.append(f"- 진행 중 스토리 {st.get('active', 0)}건 · 검토 대기 {st.get('review_open', 0)}건 (out/stories.html)")
    return L


def _t(d) -> str:
    return f"{d:%m/%d}({_WD[d.weekday()]}) {d:%H:%M}" if d else "-"


def to_markdown(rep: Report) -> str:
    L = [f"# 메일 브리핑 — {rep.generated:%Y-%m-%d %H:%M}", ""]
    L += ["## 오늘의 핵심", rep.briefing, ""]

    L.append(f"## 놓치고 있는 일 ({len(rep.missed)})")
    if not rep.missed:
        L.append("- 없음")
    for f in rep.missed:
        key = f" `done {f.key}`" if f.key else ""
        L.append(f"- **[{_KIND_KO.get(f.kind, f.kind)}]** {_t(f.when)} — {f.title}  \n  {f.detail}{key}")
    L.append("")

    if rep.attention:
        L.append(f"## 응답이 필요한 회의 초대 ({len(rep.attention)})")
        for f in rep.attention:
            L.append(f"- {_t(f.when)} — {f.title} ({f.detail})")
        L.append("")

    L.append(f"## 다가오는 일정 ({len(rep.upcoming_events)})")
    for e in rep.upcoming_events:
        tag = " (미정)" if e.response_status == "tentative" else ""
        where = f" @ {e.location}" if e.location else ""
        when = f"{e.start:%m/%d}({_WD[e.start.weekday()]}) 종일" if e.is_all_day else _t(e.start)
        L.append(f"- {when} — {e.subject}{where}{tag}")
    if not rep.upcoming_events:
        L.append("- 없음")
    L.append("")

    if rep.upcoming:
        L.append(f"## 다가오는 마감 / 캘린더 미등록 일정 ({len(rep.upcoming)})")
        for f in rep.upcoming:
            key = f" `done {f.key}`" if f.key else ""
            L.append(f"- **[{_KIND_KO.get(f.kind, f.kind)}]** {_t(f.when)} — {f.title}  \n  {f.detail}{key}")
        L.append("")

    if rep.stories:
        L += ["## 스토리", *story_lines(rep.stories), ""]

    L.append(f"## 데일리 메일 요약 ({len(rep.digest)})")
    for d in rep.digest:
        m, i = d.mail, d.insight
        flags = (" 📎" if m.has_attachments else "") + ("" if i.by_llm else " (규칙 기반 요약)")
        L.append(f"- {_PRIO[i.priority]} [{cat_label(i)}] **{m.subject}**{thread_mark(d)} — "
                 f"{m.sender_name or m.sender}, {m.received:%H:%M}{flags}  \n  {i.summary}")
    L += ["", f"<sub>stats: {rep.stats}</sub>"]
    return "\n".join(L)


def to_html(rep: Report) -> str:
    e = html.escape
    sec = lambda title: f'<h3 style="margin:18px 0 6px;border-bottom:1px solid #ddd">{e(title)}</h3>'
    li = lambda s: f'<li style="margin:4px 0">{s}</li>'
    H = [f'<div style="font-family:Malgun Gothic,Segoe UI,sans-serif;font-size:14px;max-width:760px">',
         f'<h2>메일 브리핑 — {rep.generated:%Y-%m-%d %H:%M}</h2>',
         sec("오늘의 핵심"),
         "<ul>" + "".join(li(e(x.lstrip("- "))) for x in rep.briefing.splitlines() if x.strip()) + "</ul>",
         sec(f"놓치고 있는 일 ({len(rep.missed)})"), "<ul>"]
    H += [li(f'<b style="color:#c0392b">[{e(_KIND_KO.get(f.kind, f.kind))}]</b> {e(_t(f.when))} — {e(f.title)}'
             f'<br><span style="color:#666">{e(f.detail)}</span>') for f in rep.missed] or [li("없음")]
    H.append("</ul>")
    if rep.attention:
        H += [sec(f"응답이 필요한 회의 초대 ({len(rep.attention)})"), "<ul>"]
        H += [li(f"{e(_t(f.when))} — {e(f.title)}") for f in rep.attention]
        H.append("</ul>")
    H += [sec(f"다가오는 일정 ({len(rep.upcoming_events)})"), "<ul>"]
    H += [li(f"{e(_t(ev.start))} — {e(ev.subject)}" + (f" @ {e(ev.location)}" if ev.location else ""))
          for ev in rep.upcoming_events] or [li("없음")]
    H.append("</ul>")
    if rep.upcoming:
        H += [sec(f"다가오는 마감 / 캘린더 미등록 일정 ({len(rep.upcoming)})"), "<ul>"]
        H += [li(f"<b>[{e(_KIND_KO.get(f.kind, f.kind))}]</b> {e(_t(f.when))} — {e(f.title)}"
                 f'<br><span style="color:#666">{e(f.detail)}</span>') for f in rep.upcoming]
        H.append("</ul>")
    if rep.stories:
        H += [sec("스토리"), "<ul>"] + [li(e(x.lstrip("- "))) for x in story_lines(rep.stories)] + ["</ul>"]
    H += [sec(f"데일리 메일 요약 ({len(rep.digest)})"), "<ul>"]
    for d in rep.digest:
        H.append(li(f"{_PRIO[d.insight.priority]} [{e(cat_label(d.insight))}] "
                    f"<b>{e(d.mail.subject)}</b>{e(thread_mark(d))} — {e(d.mail.sender_name or d.mail.sender)}, "
                    f"{d.mail.received:%H:%M}<br>{e(d.insight.summary)}"))
    H.append("</ul></div>")
    return "\n".join(H)
