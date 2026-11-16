"""Desktop popup with swipeable cards (tkinter, part of the standard Windows Python install).

Controls: ←/→ keys, ◀ ▶ buttons, mouse wheel, Esc to close. "전체 보기" opens the full HTML report.
"""
from __future__ import annotations

import logging
import os
import webbrowser
from pathlib import Path

from .cards import Card, CardItem

log = logging.getLogger(__name__)
W, H = 440, 600
FONT = "Malgun Gothic" if os.name == "nt" else "TkDefaultFont"


def show_cards(cards: list[Card], title: str = "메일 브리핑", full_report: str | None = None,
               timeout_s: int = 0) -> bool:
    """Show the popup and block until it is closed. Return False if no display is available."""
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except ImportError:
        log.warning("tkinter is not available: popup skipped")
        return False
    try:
        root = tk.Tk()
    except tk.TclError as e:  # no desktop session (e.g. task runs 'whether user is logged on or not')
        log.warning("no display for popup: %s", e)
        return False

    root.title(title)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{sw - W - 24}+{max(0, sh - H - 72)}")  # bottom-right, above the taskbar
    root.attributes("-topmost", True)
    root.configure(bg="#0f172a")
    root.resizable(False, False)
    f_title = tkfont.Font(family=FONT, size=15, weight="bold")
    f_text = tkfont.Font(family=FONT, size=10, weight="bold")
    f_sub = tkfont.Font(family=FONT, size=9)
    f_stat = tkfont.Font(family=FONT, size=18, weight="bold")

    state = {"i": 0}
    header = tk.Label(root, bg="#0f172a", fg="#cbd5e1", font=f_sub, anchor="w")
    header.pack(fill="x", padx=12, pady=(10, 4))
    body = tk.Frame(root, bg="white")
    body.pack(fill="both", expand=True, padx=12)
    nav = tk.Frame(root, bg="#0f172a")
    nav.pack(fill="x", padx=12, pady=10)

    def item_row(parent, it: CardItem, color: str) -> None:
        row = tk.Frame(parent, bg="white")
        row.pack(fill="x", pady=4)
        line = tk.Frame(row, bg="white")
        line.pack(fill="x")
        if it.tag:
            tk.Label(line, text=f" {it.tag} ", bg=color, fg="white", font=f_sub).pack(side="left", padx=(0, 6))
        tk.Label(line, text=it.text, bg="white", fg="#0f172a", font=f_text, anchor="w", justify="left",
                 wraplength=W - 90).pack(side="left", fill="x")
        if it.sub:
            sub = it.sub if len(it.sub) <= 110 else it.sub[:108] + "…"
            tk.Label(row, text=sub, bg="white", fg="#64748b", font=f_sub, anchor="w", justify="left",
                     wraplength=W - 60).pack(fill="x")
        tk.Frame(row, bg="#e2e8f0", height=1).pack(fill="x", pady=(4, 0))

    def render() -> None:
        c = cards[state["i"]]
        for w in body.winfo_children():
            w.destroy()
        tk.Frame(body, bg=c.color, height=8).pack(fill="x")
        inner = tk.Frame(body, bg="white")
        inner.pack(fill="both", expand=True, padx=16, pady=12)
        tk.Label(inner, text=c.title, bg="white", fg=c.color, font=f_title, anchor="w").pack(fill="x", pady=(0, 8))
        if c.stats:
            grid = tk.Frame(inner, bg="white")
            grid.pack(fill="x", pady=(0, 10))
            for k, (name, n) in enumerate(c.stats):
                cell = tk.Frame(grid, bg="#f1f5f9")
                cell.grid(row=0, column=k, padx=3, sticky="nsew")
                grid.columnconfigure(k, weight=1)
                tk.Label(cell, text=str(n), bg="#f1f5f9", fg="#0f172a", font=f_stat).pack(pady=(6, 0))
                tk.Label(cell, text=name, bg="#f1f5f9", fg="#475569", font=f_sub).pack(pady=(0, 6))
        for it in c.items:
            item_row(inner, it, c.color)
        if not c.items and not c.stats:
            tk.Label(inner, text="없음", bg="white", fg="#64748b", font=f_sub).pack(anchor="w")
        if c.more:
            tk.Label(inner, text=f"외 {c.more}건 — 전체 보기에서 확인", bg="white", fg="#94a3b8",
                     font=f_sub).pack(anchor="w", pady=(6, 0))
        header.config(text=f"{title}    {state['i'] + 1} / {len(cards)}")
        b_prev.config(state="normal" if state["i"] > 0 else "disabled")
        b_next.config(state="normal" if state["i"] < len(cards) - 1 else "disabled")

    def go(d: int) -> None:
        state["i"] = max(0, min(len(cards) - 1, state["i"] + d))
        render()

    style = dict(bg="#1e293b", fg="#e2e8f0", activebackground="#334155", activeforeground="white",
                 relief="flat", font=f_text, padx=12, pady=4, disabledforeground="#475569")
    b_prev = tk.Button(nav, text="◀", command=lambda: go(-1), **style)
    b_prev.pack(side="left")
    tk.Button(nav, text="닫기", command=root.destroy, **style).pack(side="right")
    b_next = tk.Button(nav, text="▶", command=lambda: go(1), **style)
    b_next.pack(side="right", padx=6)
    if full_report and Path(full_report).exists():
        tk.Button(nav, text="전체 보기", command=lambda: webbrowser.open(Path(full_report).resolve().as_uri()),
                  **style).pack(side="left", padx=6)

    root.bind("<Right>", lambda e: go(1))
    root.bind("<Left>", lambda e: go(-1))
    root.bind("<Escape>", lambda e: root.destroy())
    root.bind("<MouseWheel>", lambda e: go(-1 if e.delta > 0 else 1))
    if timeout_s > 0:
        root.after(timeout_s * 1000, root.destroy)
    render()
    root.lift()
    root.focus_force()
    root.mainloop()
    return True
