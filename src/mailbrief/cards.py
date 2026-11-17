"""Card news: the report as a few short cards. Used by the desktop popup and the HTML card page."""
from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field

from .analyze import Report

_WD = "월화수목금토일"
_KIND = {"unanswered_invite": "미응답 회의", "overdue_action": "기한 지남", "missed_mail_event": "캘린더 누락",
         "overdue_reply": "회신 지연", "upcoming_deadline": "마감", "mail_event_not_on_calendar": "캘린더 미등록"}
_CAT = {"action_required": "조치 필요", "meeting": "회의", "fyi": "참고", "announcement": "공지",
        "newsletter": "뉴스레터", "other": "기타"}
MAX_ITEMS = 6


def _t(d, with_day=True) -> str:
    if not d:
        return ""
    return f"{d:%m/%d}({_WD[d.weekday()]}) {d:%H:%M}" if with_day else f"{d:%H:%M}"


@dataclass
class CardItem:
    text: str
    sub: str = ""
    tag: str = ""


@dataclass
class Card:
    title: str
    color: str                     # accent color (hex)
    items: list[CardItem] = field(default_factory=list)
    stats: list[tuple[str, int]] = field(default_factory=list)
    more: int = 0


def _cap(card: Card, items: list[CardItem]) -> Card:
    card.items = items[:MAX_ITEMS]
    card.more = max(0, len(items) - MAX_ITEMS)
    return card


def build_cards(rep: Report) -> list[Card]:
    cards: list[Card] = []
    today = rep.generated.date()
    todays = [e for e in rep.upcoming_events if e.start.date() <= today <= e.end.date()]
    core = Card("오늘의 핵심", "#2563eb",
                [CardItem(l.lstrip("- ").strip()) for l in rep.briefing.splitlines() if l.strip()],
                [("놓친 일", len(rep.missed)), ("응답 필요", len(rep.attention)),
                 ("오늘 일정", len(todays)), ("새 메일", len(rep.digest))])
    cards.append(core)

    if rep.missed:
        cards.append(_cap(Card("놓치고 있는 일", "#dc2626"),
                          [CardItem(f.title, f"{_t(f.when)} · {f.detail}", _KIND.get(f.kind, f.kind)) for f in rep.missed]))
    if rep.attention:
        cards.append(_cap(Card("응답이 필요한 회의 초대", "#d97706"),
                          [CardItem(f.title, f"{_t(f.when)} · {f.detail}", "초대") for f in rep.attention]))
    if rep.upcoming_events:
        items = []
        for e in rep.upcoming_events:
            when = f"{e.start:%m/%d}({_WD[e.start.weekday()]}) 종일" if e.is_all_day else _t(e.start)
            tag = "오늘" if e.start.date() == today else ""
            sub = when + (f" · {e.location}" if e.location else "") + (" · 미정" if e.response_status == "tentative" else "")
            items.append(CardItem(e.subject, sub, tag))
        cards.append(_cap(Card("다가오는 일정", "#059669"), items))
    if rep.upcoming:
        cards.append(_cap(Card("마감 · 캘린더 미등록 일정", "#7c3aed"),
                          [CardItem(f.title, f"{_t(f.when)} · {f.detail}", _KIND.get(f.kind, f.kind)) for f in rep.upcoming]))
    st = rep.stories
    if st and (st.get("created") or st.get("updated") or st.get("review_new")):
        items = [CardItem(title, f"{sid} · {subj}", "새 스토리") for sid, title, subj in st.get("created", [])]
        items += [CardItem(title, f"{sid} · {subj}", "업데이트") for sid, title, subj in st.get("updated", [])]
        items += [CardItem(subj, f"{rid} · {why}", "검토") for rid, subj, why in st.get("review_new", [])]
        cards.append(_cap(Card("스토리", "#9333ea", stats=[("진행 중", st.get("active", 0)),
                                                         ("검토 대기", st.get("review_open", 0))]), items))
    imp = [d for d in rep.digest if d.insight.priority <= 2 and d.insight.category not in ("newsletter",)]
    if imp:
        cards.append(_cap(Card("주요 메일", "#0891b2"),
                          [CardItem(d.mail.subject + (f" ({d.count}통)" if d.count > 1 else ""),
                                    f"{d.mail.sender_name or d.mail.sender} · {d.insight.summary}",
                                    _CAT.get(d.insight.category, d.insight.category)) for d in imp]))
    rest = [d for d in rep.digest if d not in imp]
    if rest:
        counts: dict[str, int] = {}
        for d in rest:
            counts[_CAT.get(d.insight.category, d.insight.category)] = counts.get(
                _CAT.get(d.insight.category, d.insight.category), 0) + 1
        cards.append(_cap(Card("그 밖의 메일", "#64748b",
                               stats=sorted(counts.items(), key=lambda x: -x[1])[:4]),
                          [CardItem(d.mail.subject, d.mail.sender_name or d.mail.sender,
                                    _CAT.get(d.insight.category, "")) for d in rest]))
    return cards


def save_cards(cards: list[Card], path, title: str = "", full_report: str = ""):
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"title": title, "full_report": full_report, "cards": [asdict(c) for c in cards]},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_cards(path) -> tuple[list[Card], str, str]:
    from pathlib import Path
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    cards = [Card(c["title"], c["color"], [CardItem(**i) for i in c["items"]],
                  [tuple(s) for s in c["stats"]], c.get("more", 0)) for c in d["cards"]]
    return cards, d.get("title", ""), d.get("full_report", "")


def to_cards_html(rep: Report) -> str:
    """Self-contained page. Move with ←/→ keys, buttons, mouse wheel or touch swipe."""
    cards = [asdict(c) for c in build_cards(rep)]
    data = json.dumps(cards, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(f"메일 브리핑 {rep.generated:%m/%d %H:%M}")
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#0f172a;font-family:"Malgun Gothic","Segoe UI",sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh}}
.wrap{{width:420px;max-width:100vw}} .head{{color:#cbd5e1;font-size:13px;margin:0 6px 8px;display:flex;justify-content:space-between}}
.card{{background:#fff;border-radius:16px;min-height:520px;padding:22px;border-top:8px solid var(--c);
box-shadow:0 10px 30px rgba(0,0,0,.35);transition:opacity .15s}}
h1{{font-size:20px;margin:0 0 14px;color:var(--c)}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}}
.stat{{background:#f1f5f9;border-radius:10px;padding:10px 4px;text-align:center}}
.stat b{{display:block;font-size:22px;color:#0f172a}} .stat span{{font-size:11px;color:#475569}}
.item{{padding:9px 0;border-bottom:1px solid #e2e8f0}} .item:last-child{{border:0}}
.text{{font-size:14px;font-weight:600;color:#0f172a}} .sub{{font-size:12px;color:#64748b;margin-top:3px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.tag{{display:inline-block;font-size:11px;color:#fff;background:var(--c);border-radius:6px;padding:1px 6px;margin-right:6px}}
.more{{font-size:12px;color:#94a3b8;margin-top:8px}}
.nav{{display:flex;justify-content:space-between;align-items:center;margin-top:12px}}
button{{background:#1e293b;color:#e2e8f0;border:0;border-radius:10px;padding:8px 16px;font-size:14px;cursor:pointer}}
button:disabled{{opacity:.3}} .dots span{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#475569;margin:0 3px}}
.dots span.on{{background:#e2e8f0}}
</style></head><body><div class="wrap">
<div class="head"><span>{title}</span><span id="pos"></span></div>
<div class="card" id="card"></div>
<div class="nav"><button id="prev">◀</button><div class="dots" id="dots"></div><button id="next">▶</button></div>
</div><script>
const CARDS={data};let i=0;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c]));
function render(){{const c=CARDS[i],el=document.getElementById('card');el.style.setProperty('--c',c.color);
let h=`<h1>${{esc(c.title)}}</h1>`;
if(c.stats.length)h+='<div class="stats">'+c.stats.map(s=>`<div class="stat"><b>${{s[1]}}</b><span>${{esc(s[0])}}</span></div>`).join('')+'</div>';
h+=c.items.map(t=>`<div class="item"><div class="text">${{t.tag?`<span class="tag">${{esc(t.tag)}}</span>`:''}}${{esc(t.text)}}</div>${{t.sub?`<div class="sub">${{esc(t.sub)}}</div>`:''}}</div>`).join('');
if(!c.items.length&&!c.stats.length)h+='<div class="sub">없음</div>';
if(c.more)h+=`<div class="more">외 ${{c.more}}건 — 전체 리포트에서 확인</div>`;
el.innerHTML=h;document.getElementById('pos').textContent=`${{i+1}} / ${{CARDS.length}}`;
document.getElementById('prev').disabled=i==0;document.getElementById('next').disabled=i==CARDS.length-1;
document.getElementById('dots').innerHTML=CARDS.map((_,k)=>`<span class="${{k==i?'on':''}}"></span>`).join('');}}
function go(d){{i=Math.max(0,Math.min(CARDS.length-1,i+d));render();}}
document.getElementById('prev').onclick=()=>go(-1);document.getElementById('next').onclick=()=>go(1);
document.addEventListener('keydown',e=>{{if(e.key=='ArrowRight')go(1);if(e.key=='ArrowLeft')go(-1);}});
let x0=null;document.addEventListener('touchstart',e=>x0=e.touches[0].clientX);
document.addEventListener('touchend',e=>{{if(x0===null)return;const dx=e.changedTouches[0].clientX-x0;if(Math.abs(dx)>40)go(dx<0?1:-1);x0=null;}});
let wl=0;document.addEventListener('wheel',e=>{{const n=Date.now();if(n-wl<350)return;wl=n;go(e.deltaY>0||e.deltaX>0?1:-1);}});
render();
</script></body></html>"""
