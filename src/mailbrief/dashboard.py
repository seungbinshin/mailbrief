"""Story views: the HTML dashboard (out/stories.html), the story folder export, and safe folder names.

Folder export layout (stories.export_dir):
  <dir>/README.md                  index (generated)
  <dir>/active/S3 Q4 MPE 검증/      one folder per story. STORY.md and .mailbrief are generated.
  <dir>/done/S1 .../                Other files in a story folder are yours: the export never deletes them.
  <dir>/merged/S4 .../              When the status changes, the folder is moved, not deleted.
  <dir>/review/R2 ....md            open review items (generated; removed when the item is closed)
Notices, newsletters and FYI mails are not exported: they are mails, not stories.
"""
from __future__ import annotations

import html
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .stories import ReviewItem, Story, StoryTracker, reason_ko

MARK = "<!-- mailbrief:generated -->"
STATUS_DIR = {"active": "active", "done": "done", "merged": "merged"}
STATUS_KO = {"active": "진행 중", "done": "완료", "merged": "병합됨"}
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str, max_len: int = 60, fallback: str = "untitled") -> str:
    """Folder/file name that is safe on Windows and Linux. 'Q4/Q1 계획' -> 'Q4-Q1 계획'. No path parts."""
    s = unicodedata.normalize("NFC", name or "")
    s = _BAD_CHARS.sub("-", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"-{2,}", "-", s).strip(" .-")
    if s.split(".")[0].strip().upper() in _RESERVED:
        s = "_" + s
    s = s[:max_len].rstrip(" .-")
    return s or fallback


def owner_label(owner: str) -> str:
    return "나" if owner.strip().lower() == "me" else owner


_CAT_KO = {"action_required": "조치 필요", "meeting": "회의", "fyi": "참고", "announcement": "공지",
           "newsletter": "뉴스레터", "other": "기타"}


def story_dirname(st: Story) -> str:
    return f"{st.sid} {safe_name(st.title, fallback='제목 없음')}"


def _local(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- folder export

def _story_md(tr: StoryTracker, st: Story, tz: ZoneInfo) -> str:
    L = [MARK, f"# {st.sid} {st.title}", "",
         f"- 상태: {STATUS_KO.get(st.status, st.status)}",
         f"- 목표: {st.goal or '-'}",
         f"- 담당: {owner_label(st.owner) or '(미확인)'}",
         f"- 종결 조건: {st.end_condition or '(미확인)'}",
         f"- 마지막 활동: {_local(st.updated, tz)}"]
    reasons = st.review_reasons()
    if reasons:
        L.append(f"- 검토 사유: {', '.join(reason_ko(r) for r in reasons)}")
    L += ["", "## 업데이트 (최신순)"]
    for u in tr.updates(st.id):
        L.append(f"- {_local(u['source_time'], tz)} · {u['subject']} — {u['summary']}")
    L += ["", "## 연결된 스레드"] + [f"- `{l['key']}` ({l['how']})" for l in tr.links(st.id)]
    return "\n".join(L) + "\n"


def _review_md(item: ReviewItem, tz: ZoneInfo) -> str:
    p = item.proposal
    return "\n".join([
        MARK, f"# {item.rid} {item.subject}", "",
        f"- 받은 시각: {_local(item.source_time, tz)}",
        f"- 요약: {item.summary}",
        f"- 검토 사유: {', '.join(reason_ko(r) for r in item.reasons)}",
        f"- 제안 제목: {p.get('title', '')}",
        f"- 제안 목표: {p.get('goal') or '-'}",
        f"- 제안 담당: {owner_label(p.get('owner') or '') or '(미확인)'}",
        f"- 제안 종결 조건: {p.get('end_condition') or '(미확인)'}",
        "",
        "처리:",
        f"- 스토리로 등록: `mailbrief story promote {item.rid} --owner \"담당\" --end \"종결 조건\"`",
        f"- 기존 스토리에 연결: `mailbrief story promote {item.rid} --into S<번호>`",
        f"- 스토리 아님: `mailbrief story dismiss {item.rid}`",
    ]) + "\n"


def _head(p: Path, n: int = 64) -> bytes:
    """First bytes of a file, or b"" if it cannot be read. Files in the export folder can be the user's own
    (any encoding, locked by an editor): never fail on them."""
    try:
        with open(p, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def _find_story_dir(root: Path, sid: str) -> Path | None:
    for sub in STATUS_DIR.values():
        d = root / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_dir() and _head(p / ".mailbrief").decode("utf-8", "ignore").strip() == sid:
                return p
    return None


def export_folders(tr: StoryTracker, root: str | Path, tz: ZoneInfo) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stories = tr.stories(("active", "done", "merged"))
    for st in stories:
        target = root / STATUS_DIR[st.status] / story_dirname(st)
        cur = _find_story_dir(root, st.sid)
        if cur and cur != target and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur), str(target))  # status or title changed: move with the user's files
        elif cur and cur != target:
            target = cur
        target.mkdir(parents=True, exist_ok=True)
        (target / ".mailbrief").write_text(st.sid, encoding="utf-8")
        (target / "STORY.md").write_text(_story_md(tr, st, tz), encoding="utf-8")

    rv = root / "review"
    rv.mkdir(exist_ok=True)
    items = tr.open_reviews()
    keep = {f"{i.rid} {safe_name(i.subject, fallback='제목 없음')}.md" for i in items}
    for p in rv.glob("*.md"):
        if p.name not in keep and _head(p).startswith(MARK.encode()):
            try:
                p.unlink()  # closed review item; only generated files are removed
            except OSError:
                pass
    for i in items:
        (rv / f"{i.rid} {safe_name(i.subject, fallback='제목 없음')}.md").write_text(_review_md(i, tz), encoding="utf-8")

    L = [MARK, "# 스토리 목록", "", f"## 검토 대기 ({len(items) + len(tr.needs_review())})"]
    L += [f"- [{i.rid}] {i.subject} — {', '.join(reason_ko(r) for r in i.reasons)}" for i in items]
    L += [f"- [{s.sid}] {s.title} — {', '.join(reason_ko(r) for r in s.review_reasons())}" for s in tr.needs_review()]
    for status in ("active", "done"):
        group = [s for s in stories if s.status == status]
        L += ["", f"## {STATUS_KO[status]} ({len(group)})"]
        L += [f"- [{s.sid}] {s.title} · 담당 {owner_label(s.owner) or '?'} · {_local(s.updated, tz)}" for s in group]
    (root / "README.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------- HTML dashboard

_CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--ink:#0f172a;--muted:#5b6474;--line:#e3e6eb;--accent:#2563eb;
--warn:#b45309;--warnbg:#fef3c7;--ok:#047857;--okbg:#d1fae5;--chip:#eef2f7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f141b;--panel:#171d26;--ink:#e6e9ee;
--muted:#9aa4b2;--line:#2a323e;--accent:#7aa2ff;--warn:#fbbf24;--warnbg:#3a2e12;--ok:#34d399;--okbg:#0f3326;--chip:#222a35}}
:root[data-theme="dark"]{--bg:#0f141b;--panel:#171d26;--ink:#e6e9ee;--muted:#9aa4b2;--line:#2a323e;--accent:#7aa2ff;
--warn:#fbbf24;--warnbg:#3a2e12;--ok:#34d399;--okbg:#0f3326;--chip:#222a35}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Malgun Gothic","Segoe UI",system-ui,sans-serif;
word-break:keep-all}
header{padding:20px 16px 8px;max-width:1280px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px} .sub{color:var(--muted);font-size:12px}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:8px;margin:14px 0 4px;max-width:880px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.stat b{display:block;font-size:20px} .stat span{font-size:12px;color:var(--muted)}
.tools{margin:10px 0 0} input{width:100%;max-width:360px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);
background:var(--panel);color:var(--ink);font:inherit}
main{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;padding:8px 16px 32px;max-width:1280px;margin:0 auto}
@media (max-width:900px){main{grid-template-columns:1fr}}
.col h2{font-size:15px;margin:8px 0 10px;display:flex;justify-content:space-between}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.card h3{font-size:14px;margin:0 0 4px;overflow-wrap:anywhere} .id{color:var(--accent);font-weight:700;margin-right:6px}
.meta{font-size:12px;color:var(--muted);margin:2px 0;overflow-wrap:anywhere}
.chips{margin:6px 0 2px} .chip{display:inline-block;font-size:11px;border-radius:6px;padding:1px 7px;margin:0 4px 4px 0;
background:var(--warnbg);color:var(--warn)} .chip.ok{background:var(--okbg);color:var(--ok)}
.last{font-size:13px;margin-top:6px;overflow-wrap:anywhere}
details{margin-top:6px;font-size:12px;color:var(--muted)} details li{margin:2px 0}
code{font-size:11px;background:var(--chip);border-radius:4px;padding:1px 4px;overflow-wrap:anywhere}
.empty{color:var(--muted);font-size:13px}
"""


def _story_card(tr: StoryTracker, st: Story, tz: ZoneInfo) -> str:
    e = html.escape
    ups = tr.updates(st.id, 20)
    reasons = st.review_reasons()
    chips = "".join(f'<span class="chip">{e(reason_ko(r))}</span>' for r in reasons) or (
        '<span class="chip ok">확인됨</span>' if st.status == "active" else "")
    last = f'<div class="last">{e(ups[0]["summary"])}</div>' if ups else ""
    hist = "".join(f"<li>{e(_local(u['source_time'], tz))} · {e(u['subject'])} — {e(u['summary'])}</li>" for u in ups)
    hint = ""
    if "owner_unknown" in reasons or "end_unknown" in reasons:
        hint = f'<div class="meta"><code>mailbrief story set {st.sid} --owner "담당" --end "종결 조건"</code></div>'
    elif reasons:
        hint = f'<div class="meta"><code>mailbrief story confirm {st.sid}</code> · <code>mailbrief story close {st.sid}</code></div>'
    return (f'<div class="card" data-q="{e((st.title + " " + st.goal + " " + st.owner).lower())}">'
            f'<h3><span class="id">{st.sid}</span>{e(st.title)}</h3>'
            f'<div class="meta">목표: {e(st.goal or "-")}</div>'
            f'<div class="meta">담당: {e(owner_label(st.owner) or "(미확인)")} · 종결: {e(st.end_condition or "(미확인)")}</div>'
            f'<div class="meta">마지막 활동 {e(_local(st.updated, tz))} · 업데이트 {len(tr.updates(st.id))}건</div>'
            f'<div class="chips">{chips}</div>{last}{hint}'
            f'<details><summary>업데이트 이력</summary><ul>{hist}</ul></details></div>')


def _review_card(item: ReviewItem, tz: ZoneInfo) -> str:
    e = html.escape
    p = item.proposal
    chips = "".join(f'<span class="chip">{e(reason_ko(r))}</span>' for r in item.reasons)
    return (f'<div class="card" data-q="{e((item.subject + " " + item.summary).lower())}">'
            f'<h3><span class="id">{item.rid}</span>{e(item.subject)}</h3>'
            f'<div class="meta">{e(_local(item.source_time, tz))} · 분류 {e(_CAT_KO.get(item.category, item.category))}</div>'
            f'<div class="chips">{chips}</div><div class="last">{e(item.summary)}</div>'
            f'<div class="meta">제안: {e(p.get("title", ""))} · 담당 {e(owner_label(p.get("owner") or "") or "?")} · '
            f'종결 {e(p.get("end_condition") or "?")}</div>'
            f'<div class="meta"><code>mailbrief story promote {item.rid} --owner "담당" --end "종결 조건"</code> · '
            f'<code>mailbrief story dismiss {item.rid}</code></div></div>')


def to_dashboard_html(tr: StoryTracker, tz: ZoneInfo, now: datetime, run_stats: dict | None = None) -> str:
    e = html.escape
    reviews, flagged = tr.open_reviews(), tr.needs_review()
    active = [s for s in tr.stories(("active",)) if not s.review_reasons()]
    done = [s for s in tr.stories(("done",)) if not s.review_reasons()][:20]
    rs = run_stats or {}
    stats = [("진행 중", len(active) + sum(s.status == "active" for s in flagged)), ("검토 대기", len(reviews) + len(flagged)),
             ("완료", len(tr.stories(("done",))))]
    if rs.get("threads"):
        share = rs.get("story", 0) / rs["threads"]
        stats += [("이번 실행 스레드", rs["threads"]), ("스토리 판정", f"{rs.get('story', 0)} ({share:.0%})"),
                  ("검토 판정", rs.get("review", 0)), ("스토리 아님", rs.get("none", 0))]
    col = lambda title, cards: (f'<section class="col"><h2><span>{e(title)}</span><span>{len(cards)}</span></h2>'
                                + ("".join(cards) or '<div class="empty">없음</div>') + "</section>")
    review_cards = [_review_card(i, tz) for i in reviews] + [_story_card(tr, s, tz) for s in flagged]
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Story Tracker</title>
<style>{_CSS}</style></head><body>
<header><h1>스토리 트래커</h1><div class="sub">갱신 {now:%Y-%m-%d %H:%M} · 스토리 = 계속 추적할 목표 1개 (지속성 + 담당 + 종결 조건)</div>
<div class="stats">{"".join(f'<div class="stat"><b>{e(str(v))}</b><span>{e(k)}</span></div>' for k, v in stats)}</div>
<div class="tools"><input id="q" placeholder="검색: 제목, 목표, 담당" aria-label="검색"></div></header>
<main>{col("검토 대기", review_cards)}{col("진행 중", [_story_card(tr, s, tz) for s in active])}{col("최근 완료", [_story_card(tr, s, tz) for s in done])}</main>
<script>
document.getElementById('q').addEventListener('input',ev=>{{const q=ev.target.value.trim().toLowerCase();
document.querySelectorAll('.card').forEach(c=>{{c.style.display=!q||c.dataset.q.includes(q)?'':'none';}});}});
</script></body></html>"""
