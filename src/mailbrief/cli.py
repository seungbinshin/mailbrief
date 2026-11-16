"""Command line.

  mailbrief run          full briefing (files + card popup)
  mailbrief watch        stay running: new mail -> analyze + popup, brief_times -> full briefing
  mailbrief popup [JSON] show the card popup again (default: latest briefing)
  mailbrief diagnose     classification failure analysis
  mailbrief done KEY     mark an action item as done
  mailbrief check-llm    test the LLM endpoint
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import expand, load_config


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Korean output on Windows consoles
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="mailbrief", description="Outlook mail/calendar briefing with an in-house LLM")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", help="also write the log to this file")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="build and deliver the briefing")
    r.add_argument("--no-llm", action="store_true", help="use rule-based extraction only")
    r.add_argument("--no-deliver", action="store_true", help="print Markdown to stdout only")
    r.add_argument("--no-popup", action="store_true")
    r.add_argument("--now", help="override current time (ISO 8601), for tests")
    r.add_argument("--workers", type=int, default=4, help="parallel LLM calls")

    w = sub.add_parser("watch", help="process new mails as they arrive")
    w.add_argument("--once", action="store_true", help="one poll, then exit")
    w.add_argument("--no-popup", action="store_true")

    pp = sub.add_parser("popup", help="show the card popup")
    pp.add_argument("cards_json", nargs="?")

    dg = sub.add_parser("diagnose", help="analyze classification failures")
    dg.add_argument("--sample", type=int, default=0, help="classify the last N mails now")
    dg.add_argument("--retry", action="store_true", help="re-run mails that failed before")
    dg.add_argument("--days", type=int, default=7)

    d = sub.add_parser("done", help="mark an action item as done")
    d.add_argument("key")

    sub.add_parser("check-llm", help="send a test prompt to the LLM endpoint")

    args = p.parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()] if sys.stderr else []  # pythonw has no console
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    if args.cmd == "popup":
        from .cards import load_cards
        from .popup import show_cards

        path = args.cards_json
        if not path:
            cfg = load_config(args.config)
            path = str(Path(cfg.output.dir) / "latest_cards.json")
        cards, title, full = load_cards(path)
        show_cards(cards, title or "메일 브리핑", full)
        return 0

    cfg = load_config(args.config)
    if args.cmd == "run":
        from .pipeline import run
        from .report import to_markdown

        now = datetime.fromisoformat(args.now) if args.now else None
        rep = run(cfg, now=now, use_llm=not args.no_llm, deliver=not args.no_deliver, workers=args.workers)
        if args.no_deliver:
            print(to_markdown(rep))
            return 0
        print(f"OK: missed={len(rep.missed)} upcoming_events={len(rep.upcoming_events)} "
              f"digest={len(rep.digest)} llm_errors={rep.stats.get('llm_errors')} -> {cfg.output.dir}")
        if cfg.output.popup and not args.no_popup and rep.stats.get("cards_json"):
            from .cards import load_cards
            from .popup import show_cards
            cards, title, full = load_cards(rep.stats["cards_json"])
            show_cards(cards, title, full)
        if cfg.output.open_html and rep.stats.get("cards_path"):
            import webbrowser
            webbrowser.open(Path(rep.stats["cards_path"]).as_uri())
        return 0
    if args.cmd == "watch":
        from .watch import watch

        watch(cfg, once=args.once, popup=not args.no_popup)
        return 0
    if args.cmd == "diagnose":
        from .diagnose import diagnose

        diagnose(cfg, sample=args.sample, retry=args.retry, days=args.days)
        return 0
    if args.cmd == "done":
        from .store import Store

        Store(expand(cfg.output.db_path)).set_action_status(args.key, "done")
        print(f"marked done: {args.key}")
        return 0
    if args.cmd == "check-llm":
        from .llm import OpenAICompatClient, parse_json

        out = OpenAICompatClient(cfg.llm).chat("Return JSON only.", 'Return {"ok": true}.', json_output=True)
        print(out)
        print("parsed:", parse_json(out))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
